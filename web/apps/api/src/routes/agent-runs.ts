import { Hono } from 'hono'
import type { Env } from '../app'
import { authMiddleware, type AuthContext } from '../middleware/auth'
import { whitelistMiddleware } from '../middleware/whitelist'
import {
  AGENT_RUN_INPUT_SCHEMA,
  AgentRunServiceError,
  type AgentRunInput,
  runPythonResearch,
} from '../services/agent-run'
import { createAgentRunStore } from '../services/agent-run-store'

type AgentRunBindings = { Bindings: Env; Variables: { auth: AuthContext } }

export const agentRunRoutes = new Hono<AgentRunBindings>()

agentRunRoutes.use('*', authMiddleware)
agentRunRoutes.use('*', whitelistMiddleware)

agentRunRoutes.post('/', async (c) => {
  if (c.env.AGENT_SANDBOX_ENABLED !== 'true') return c.json({ error: 'Agent sandbox is disabled' }, 503)
  const parsed = parseAgentRunInput(await c.req.json().catch(() => null))
  if ('error' in parsed) return c.json(parsed, 400)

  try {
    const record = await runPythonResearch(c.env, c.get('auth').userId, parsed.data.script)
    return c.json(record, 201)
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

agentRunRoutes.delete('/:id', async (c) => {
  const store = createAgentRunStore(c.env)
  if (!store) return c.json({ error: 'Agent run storage is unavailable' }, 503)
  await store.remove(c.get('auth').userId, c.req.param('id'))
  return c.body(null, 204)
})
