import { z } from 'zod'
import type { Env } from '../app'
import { createAgentRunStore, type AgentRunRecord, type AgentRunStore } from './agent-run-store'
import { executePythonSandbox, type PythonSandboxResult } from './python-sandbox'
import {
  logSandboxRun,
  safeRequestId,
  type SandboxExecutionContext,
  type SandboxRunLogger,
} from './sandbox-observability'

export const PYTHON_RESEARCH_SCRIPT_SCHEMA = z.string().trim().min(1).max(12_000)

export const AGENT_RUN_INPUT_SCHEMA = z.object({
  kind: z.literal('python_research'),
  script: PYTHON_RESEARCH_SCRIPT_SCHEMA,
})

export type AgentRunInput = z.infer<typeof AGENT_RUN_INPUT_SCHEMA>

export type AgentRunMessage = {
  kind: 'python_research'
  runId: string
  userId: string
  script: string
  requestId?: string
}

export type AgentRunOutcome = 'ack' | 'retry'

type SandboxExecutor = (env: Env, script: string, context: SandboxExecutionContext) => Promise<PythonSandboxResult>
type AgentRunQueue = { send: (message: AgentRunMessage) => Promise<unknown> }

export type AgentRunDependencies = {
  createStore?: (env: Env) => AgentRunStore | null
  executeSandbox?: SandboxExecutor
  log?: SandboxRunLogger
  queue?: AgentRunQueue
  requestId?: string
}

export class AgentRunServiceError extends Error {
  constructor(
    message: string,
    readonly status: 503,
    readonly record?: AgentRunRecord,
  ) {
    super(message)
  }
}

export async function enqueuePythonResearch(
  env: Env,
  userId: string,
  script: string,
  dependencies: AgentRunDependencies = {},
): Promise<AgentRunRecord> {
  if (env.AGENT_SANDBOX_ENABLED !== 'true') throw new AgentRunServiceError('Agent sandbox is disabled', 503)
  const store = storeFor(env, dependencies)
  const record = newRunRecord()
  const requestId = safeRequestId(dependencies.requestId)
  const context = { requestId, runId: record.id }
  const log = dependencies.log || logSandboxRun

  try {
    await store.save(userId, record)
  } catch {
    log('failed', { ...context, errorCode: 'storage_unavailable', status: 'failed' })
    throw new AgentRunServiceError('Agent run storage is unavailable', 503, record)
  }

  const queue = dependencies.queue || env.AGENT_RUN_QUEUE
  if (!queue) return failQueueDelivery(store, userId, record, context, log)
  try {
    await queue.send({ kind: 'python_research', runId: record.id, userId, script, requestId })
  } catch {
    return failQueueDelivery(store, userId, record, context, log)
  }

  log('queued', { ...context, attempts: record.attempts })
  return record
}

export async function consumePythonResearch(
  env: Env,
  message: AgentRunMessage,
  dependencies: AgentRunDependencies = {},
): Promise<AgentRunOutcome> {
  const store = storeFor(env, dependencies)
  const log = dependencies.log || logSandboxRun
  const context = { requestId: safeRequestId(message.requestId), runId: message.runId }
  if (!(await store.acquireLease(message.userId, message.runId))) return 'retry'

  try {
    const record = await store.claim(message.userId, message.runId)
    if (!record) return 'ack'
    if (env.AGENT_SANDBOX_ENABLED !== 'true') {
      await failConfiguration(store, message.userId, record, context, log, 'Agent sandbox is disabled', Date.now(), 'sandbox_disabled')
      return 'ack'
    }
    return await executeQueuedRun(store, message, record, env, context, log, dependencies.executeSandbox || executePythonSandbox)
  } finally {
    await store.releaseLease(message.userId, message.runId)
  }
}

export async function failDeadLetterAgentRun(
  env: Env,
  message: AgentRunMessage,
  dependencies: AgentRunDependencies = {},
): Promise<void> {
  const store = storeFor(env, dependencies)
  const record = await store.get(message.userId, message.runId)
  if (!record || isTerminal(record)) return
  const context = { requestId: safeRequestId(message.requestId), runId: message.runId }
  const failed = await store.fail(message.userId, record, 'Agent run exhausted retries')
  if (failed) (dependencies.log || logSandboxRun)('failed', {
    ...context,
    attempts: failed.attempts,
    errorCode: 'retry_exhausted',
    status: 'failed',
  })
}

export function isAgentRunMessage(value: unknown): value is AgentRunMessage {
  if (!value || typeof value !== 'object') return false
  const message = value as Record<string, unknown>
  return message.kind === 'python_research'
    && typeof message.runId === 'string'
    && typeof message.userId === 'string'
    && typeof message.script === 'string'
    && (message.requestId === undefined || typeof message.requestId === 'string')
}

async function failQueueDelivery(
  store: AgentRunStore,
  userId: string,
  record: AgentRunRecord,
  context: SandboxExecutionContext,
  log: SandboxRunLogger,
): Promise<never> {
  const failed = await store.fail(userId, record, 'Agent run queue is unavailable').catch(() => null)
  const visibleRecord = failed || failRun(record, 'Agent run queue is unavailable')
  log('failed', { ...context, errorCode: 'queue_delivery_failed', status: 'failed' })
  throw new AgentRunServiceError('Agent run queue is unavailable', 503, visibleRecord)
}

async function executeQueuedRun(
  store: AgentRunStore,
  message: AgentRunMessage,
  record: AgentRunRecord,
  env: Env,
  context: SandboxExecutionContext,
  log: SandboxRunLogger,
  executeSandbox: SandboxExecutor,
): Promise<AgentRunOutcome> {
  const startedAt = Date.now()
  log('started', { ...context, attempts: record.attempts })
  try {
    const result = await executeSandbox(env, message.script, context)
    const completed = completeRun(record, result)
    await store.save(message.userId, completed)
    log('finished', {
      ...context,
      durationMs: Date.now() - startedAt,
      attempts: completed.attempts,
      exitCode: completed.exitCode,
      status: completed.status === 'completed' ? 'completed' : 'failed',
      usage: completed.usage,
    })
    return 'ack'
  } catch (error) {
    if (isConfigurationError(error)) {
      await failConfiguration(store, message.userId, record, context, log, 'Sandbox configuration is incomplete', startedAt)
      return 'ack'
    }
    const requeued = await store.requeue(message.userId, record, 'Sandbox execution failed')
    if (!requeued) throw new Error('Agent run state transition failed')
    log('retrying', {
      ...context,
      durationMs: Date.now() - startedAt,
      attempts: requeued.attempts,
      errorCode: 'sandbox_execution_failed',
      status: 'failed',
    })
    return 'retry'
  }
}

async function failConfiguration(
  store: AgentRunStore,
  userId: string,
  record: AgentRunRecord,
  context: SandboxExecutionContext,
  log: SandboxRunLogger,
  error: string,
  startedAt = Date.now(),
  errorCode: 'bridge_configuration_incomplete' | 'sandbox_disabled' = 'bridge_configuration_incomplete',
): Promise<void> {
  const failed = await store.fail(userId, record, error)
  if (!failed) throw new Error('Agent run state transition failed')
  log('failed', {
    ...context,
    durationMs: Date.now() - startedAt,
    attempts: failed.attempts,
    errorCode,
    status: 'failed',
  })
}

function storeFor(env: Env, dependencies: AgentRunDependencies): AgentRunStore {
  try {
    const store = (dependencies.createStore || createAgentRunStore)(env)
    if (store) return store
  } catch {
    // Fall through to the sanitized service error below.
  }
  throw new AgentRunServiceError('Agent run storage is unavailable', 503)
}

function newRunRecord(): AgentRunRecord {
  return {
    id: crypto.randomUUID(),
    kind: 'python_research',
    status: 'queued',
    attempts: 0,
    createdAt: new Date().toISOString(),
  }
}

function completeRun(record: AgentRunRecord, result: PythonSandboxResult): AgentRunRecord {
  return {
    ...record,
    status: result.exitCode === 0 ? 'completed' : 'failed',
    finishedAt: new Date().toISOString(),
    exitCode: result.exitCode,
    stdout: result.stdout,
    stderr: result.stderr,
    usage: {
      activeCpuUsageMs: result.activeCpuUsageMs,
      networkIngressBytes: result.networkIngressBytes,
      networkEgressBytes: result.networkEgressBytes,
    },
  }
}

function failRun(record: AgentRunRecord, error: string): AgentRunRecord {
  return { ...record, status: 'failed', finishedAt: new Date().toISOString(), error }
}

function isConfigurationError(error: unknown): boolean {
  return error instanceof Error && error.message === 'Sandbox bridge configuration is incomplete'
}

function isTerminal(record: AgentRunRecord): boolean {
  return record.status === 'completed' || record.status === 'failed' || record.status === 'cancelled'
}
