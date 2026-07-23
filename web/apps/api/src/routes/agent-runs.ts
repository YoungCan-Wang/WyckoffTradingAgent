import { Hono } from 'hono'
import type { Env } from '../app'
import { authMiddleware, type AuthContext } from '../middleware/auth'
import { whitelistMiddleware } from '../middleware/whitelist'
import {
  AGENT_RUN_INPUT_SCHEMA,
  AgentRunServiceError,
  type AgentRunInput,
  enqueuePythonResearch,
} from '../services/agent-run'
import { createAgentRunStore } from '../services/agent-run-store'
import { logSandboxRun, safeRequestId } from '../services/sandbox-observability'

type AgentRunBindings = { Bindings: Env; Variables: { auth: AuthContext } }

export const agentRunRoutes = new Hono<AgentRunBindings>()

agentRunRoutes.use('*', authMiddleware)
agentRunRoutes.use('*', whitelistMiddleware)

agentRunRoutes.post('/', async (c) => {
  if (c.env.AGENT_SANDBOX_ENABLED !== 'true') return c.json({ error: 'Agent sandbox is disabled' }, 503)
  const parsed = parseAgentRunInput(await c.req.json().catch(() => null))
  if ('error' in parsed) return c.json(parsed, 400)

  try {
    const record = await enqueuePythonResearch(c.env, c.get('auth').userId, parsed.data.script, {
      requestId: c.get('requestId'),
    })
    return c.json(record, 202)
  } catch (error) {
    if (error instanceof AgentRunServiceError) {
      return c.json(error.record || { error: error.message }, error.status)
    }
    return c.json({ error: 'Sandbox execution failed' }, 502)
  }
})

export function parseAgentRunInput(raw: unknown):
  | { data: AgentRunInput }
  | { error: string; details?: unknown } {
  const parsed = AGENT_RUN_INPUT_SCHEMA.safeParse(raw)
  if (!parsed.success) return { error: 'Invalid agent run', details: parsed.error.flatten() }
  return { data: parsed.data }
}

agentRunRoutes.get('/:id', async (c) => {
  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  const record = await store.get(c.get('auth').userId, c.req.param('id'))
  return record ? c.json(record) : c.json({ error: 'Agent run not found' }, 404)
})

agentRunRoutes.post('/:id/cancel', async (c) => {
  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  const userId = c.get('auth').userId
  const runId = c.req.param('id')
  const existing = await store.get(userId, runId)
  if (!existing) return c.json({ error: 'Agent run not found' }, 404)
  if (existing.status !== 'queued') return c.json({ error: 'Only queued agent runs can be cancelled' }, 409)
  const record = await store.cancel(userId, runId)
  if (!record) return c.json({ error: 'Agent run is no longer queued' }, 409)
  logSandboxRun('cancelled', {
    requestId: safeRequestId(c.get('requestId')),
    runId,
    attempts: record.attempts,
  })
  return c.json(record)
})

agentRunRoutes.delete('/:id', async (c) => {
  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  const userId = c.get('auth').userId
  const runId = c.req.param('id')
  const record = await store.get(userId, runId)
  if (!record) return c.json({ error: 'Agent run not found' }, 404)
  if (record.status === 'queued' || record.status === 'running') {
    return c.json({ error: 'Cancel an active agent run before deleting it' }, 409)
  }
  await store.remove(userId, runId)
  return c.body(null, 204)
})
