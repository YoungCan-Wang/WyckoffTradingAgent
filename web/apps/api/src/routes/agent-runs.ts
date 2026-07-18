import { Hono } from 'hono'
import { z } from 'zod'
import type { Env } from '../app'
import { authMiddleware, type AuthContext } from '../middleware/auth'
import { whitelistMiddleware } from '../middleware/whitelist'
import { createAgentRunStore, type AgentRunRecord } from '../services/agent-run-store'
import { executePythonSandbox } from '../services/python-sandbox'

type AgentRunBindings = { Bindings: Env; Variables: { auth: AuthContext } }

const AGENT_RUN_SCHEMA = z.object({
  kind: z.literal('python_research'),
  script: z.string().trim().min(1).max(12_000),
})

export const agentRunRoutes = new Hono<AgentRunBindings>()

agentRunRoutes.use('*', authMiddleware)
agentRunRoutes.use('*', whitelistMiddleware)

agentRunRoutes.post('/', async (c) => {
  if (c.env.AGENT_SANDBOX_ENABLED !== 'true') return c.json({ error: 'Agent sandbox is disabled' }, 503)
  const parsed = parseAgentRunInput(await c.req.json().catch(() => null))
  if ('error' in parsed) return c.json(parsed, 400)

  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  const userId = c.get('auth').userId
  const record = newRunRecord()
  await store.save(userId, record)

  try {
    const result = await executePythonSandbox(c.env, parsed.data.script)
    const completed = completeRun(record, result)
    await store.save(userId, completed)
    return c.json(completed, 201)
  } catch (error) {
    const failed = failRun(record, sandboxError(error))
    await store.save(userId, failed)
    return c.json(failed, failed.error === 'Sandbox configuration is incomplete' ? 503 : 502)
  }
})

export function parseAgentRunInput(raw: unknown):
  | { data: z.infer<typeof AGENT_RUN_SCHEMA> }
  | { error: string; details?: unknown } {
  const parsed = AGENT_RUN_SCHEMA.safeParse(raw)
  if (!parsed.success) return { error: 'Invalid agent run', details: parsed.error.flatten() }
  return { data: parsed.data }
}

agentRunRoutes.get('/:id', async (c) => {
  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  const record = await store.get(c.get('auth').userId, c.req.param('id'))
  return record ? c.json(record) : c.json({ error: 'Agent run not found' }, 404)
})

agentRunRoutes.delete('/:id', async (c) => {
  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  await store.remove(c.get('auth').userId, c.req.param('id'))
  return c.body(null, 204)
})

function newRunRecord(): AgentRunRecord {
  return {
    id: crypto.randomUUID(),
    kind: 'python_research',
    status: 'running',
    createdAt: new Date().toISOString(),
  }
}

function completeRun(
  record: AgentRunRecord,
  result: Awaited<ReturnType<typeof executePythonSandbox>>,
): AgentRunRecord {
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
  if (error instanceof Error && error.message === 'Vercel Sandbox env is incomplete') {
    return 'Sandbox configuration is incomplete'
  }
  return 'Sandbox execution failed'
}
