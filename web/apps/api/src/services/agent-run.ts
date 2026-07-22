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

type SandboxExecutor = (env: Env, script: string, context: SandboxExecutionContext) => Promise<PythonSandboxResult>
type SandboxFailureCode = 'bridge_configuration_incomplete' | 'sandbox_execution_failed' | 'storage_unavailable'

type AgentRunDependencies = {
  createStore?: (env: Env) => AgentRunStore | null
  executeSandbox?: SandboxExecutor
  log?: SandboxRunLogger
  requestId?: string
}

export class AgentRunServiceError extends Error {
  constructor(
    message: string,
    readonly status: 502 | 503,
    readonly record?: AgentRunRecord,
  ) {
    super(message)
  }
}

export async function runPythonResearch(
  env: Env,
  userId: string,
  script: string,
  dependencies: AgentRunDependencies = {},
): Promise<AgentRunRecord> {
  if (env.AGENT_SANDBOX_ENABLED !== 'true') throw new AgentRunServiceError('Agent sandbox is disabled', 503)
  const store = (dependencies.createStore || createAgentRunStore)(env)
  if (!store) throw new AgentRunServiceError('Agent run storage is unavailable', 503)
  const record = newRunRecord()
  const context = { requestId: safeRequestId(dependencies.requestId), runId: record.id }
  const log = dependencies.log || logSandboxRun
  const startedAt = Date.now()
  try {
    await saveRun(store, userId, record)
  } catch (error) {
    log('failed', {
      ...context,
      durationMs: Date.now() - startedAt,
      errorCode: 'storage_unavailable',
      status: 'failed',
    })
    throw error
  }
  log('started', context)
  return executeAndSaveRun(
    store,
    userId,
    record,
    env,
    script,
    context,
    startedAt,
    log,
    dependencies.executeSandbox || executePythonSandbox,
  )
}

async function executeAndSaveRun(
  store: AgentRunStore,
  userId: string,
  record: AgentRunRecord,
  env: Env,
  script: string,
  context: SandboxExecutionContext,
  startedAt: number,
  log: SandboxRunLogger,
  executeSandbox: SandboxExecutor,
): Promise<AgentRunRecord> {
  try {
    const result = await executeSandbox(env, script, context)
    const completed = completeRun(record, result)
    await saveRun(store, userId, completed)
    log('finished', {
      ...context,
      durationMs: Date.now() - startedAt,
      exitCode: completed.exitCode,
      status: completed.status === 'completed' ? 'completed' : 'failed',
      usage: completed.usage,
    })
    return completed
  } catch (error) {
    if (error instanceof AgentRunServiceError) {
      log('failed', {
        ...context,
        durationMs: Date.now() - startedAt,
        errorCode: errorCode(error),
        status: 'failed',
      })
      throw error
    }
    const failed = failRun(record, sandboxError(error))
    await store.save(userId, failed).catch(() => undefined)
    log('failed', {
      ...context,
      durationMs: Date.now() - startedAt,
      errorCode: errorCode(error),
      status: 'failed',
    })
    throw new AgentRunServiceError(failed.error || 'Sandbox execution failed', errorStatus(error), failed)
  }
}

async function saveRun(store: AgentRunStore, userId: string, record: AgentRunRecord): Promise<void> {
  try {
    await store.save(userId, record)
  } catch {
    throw new AgentRunServiceError('Agent run storage is unavailable', 503, record)
  }
}

function newRunRecord(): AgentRunRecord {
  return {
    id: crypto.randomUUID(),
    kind: 'python_research',
    status: 'running',
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

function sandboxError(error: unknown): string {
  if (error instanceof Error && error.message === 'Sandbox bridge configuration is incomplete') {
    return 'Sandbox configuration is incomplete'
  }
  return 'Sandbox execution failed'
}

function errorStatus(error: unknown): 502 | 503 {
  return error instanceof Error && error.message === 'Sandbox bridge configuration is incomplete' ? 503 : 502
}

function errorCode(error: unknown): SandboxFailureCode {
  if (error instanceof AgentRunServiceError && error.message === 'Agent run storage is unavailable') {
    return 'storage_unavailable'
  }
  if (error instanceof Error && error.message === 'Sandbox bridge configuration is incomplete') {
    return 'bridge_configuration_incomplete'
  }
  return 'sandbox_execution_failed'
}
