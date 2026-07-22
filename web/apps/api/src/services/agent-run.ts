import { z } from 'zod'
import type { Env } from '../app'
import { createAgentRunStore, type AgentRunRecord, type AgentRunStore } from './agent-run-store'
import { executePythonSandbox, type PythonSandboxResult } from './python-sandbox'

export const PYTHON_RESEARCH_SCRIPT_SCHEMA = z.string().trim().min(1).max(12_000)

export const AGENT_RUN_INPUT_SCHEMA = z.object({
  kind: z.literal('python_research'),
  script: PYTHON_RESEARCH_SCRIPT_SCHEMA,
})

export type AgentRunInput = z.infer<typeof AGENT_RUN_INPUT_SCHEMA>

type AgentRunDependencies = {
  createStore?: (env: Env) => AgentRunStore | null
  executeSandbox?: (env: Env, script: string) => Promise<PythonSandboxResult>
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
  await saveRun(store, userId, record)
  return executeAndSaveRun(store, userId, record, env, script, dependencies.executeSandbox || executePythonSandbox)
}

async function executeAndSaveRun(
  store: AgentRunStore,
  userId: string,
  record: AgentRunRecord,
  env: Env,
  script: string,
  executeSandbox: (env: Env, script: string) => Promise<PythonSandboxResult>,
): Promise<AgentRunRecord> {
  try {
    const result = await executeSandbox(env, script)
    const completed = completeRun(record, result)
    await saveRun(store, userId, completed)
    return completed
  } catch (error) {
    if (error instanceof AgentRunServiceError) throw error
    const failed = failRun(record, sandboxError(error))
    await store.save(userId, failed).catch(() => undefined)
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
