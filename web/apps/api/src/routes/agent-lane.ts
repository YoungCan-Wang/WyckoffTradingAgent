import { createMiddleware } from 'hono/factory'
import { Hono } from 'hono'
import type { Env } from '../app'
import { authMiddleware, type AuthContext } from '../middleware/auth'
import { isActivePlanetMember } from '../middleware/planet-membership'
import { agentLaneRateLimitMiddleware } from '../middleware/rate-limit'
import {
  isAgentLaneUser,
  resolveUpstream,
  streamResponse,
  toServiceLlm,
  type AgentUpstream,
  type LaneModelConfig,
} from '../services/agent-lane'
import { loadLLMConfigs, createUserSupabase } from './chat'

type LaneBindings = { Bindings: Env; Variables: { auth: AuthContext; upstream: AgentUpstream } }

type Supabase = ReturnType<typeof createUserSupabase>

export type AgentLaneDeps = {
  isMember: (supabase: Supabase, userId: string) => Promise<boolean>
  loadConfigs: (supabase: Supabase, userId: string) => Promise<LaneModelConfig[]>
  resolveUpstream: (env: Env) => AgentUpstream | null
}

const MAX_MESSAGES_CHARS = 60_000

const defaultDeps: AgentLaneDeps = {
  isMember: isActivePlanetMember,
  loadConfigs: (supabase, userId) => loadLLMConfigs(supabase, userId),
  resolveUpstream,
}

type Gate = { ok: true; upstream: AgentUpstream } | { ok: false; status: 403 | 503; error: string }

async function checkGate(env: Env, supabase: Supabase, userId: string, deps: AgentLaneDeps): Promise<Gate> {
  const upstream = deps.resolveUpstream(env)
  if (!upstream) return { ok: false, status: 503, error: 'Agent lane is not configured' }
  if (!isAgentLaneUser(env, userId)) return { ok: false, status: 403, error: 'Agent lane is not enabled for this account' }
  if (!(await deps.isMember(supabase, userId))) return { ok: false, status: 403, error: 'Planet membership required' }
  return { ok: true, upstream }
}

export function createAgentLaneRoutes(overrides: Partial<AgentLaneDeps> = {}) {
  const deps = { ...defaultDeps, ...overrides }
  const routes = new Hono<LaneBindings>()
  routes.use('*', authMiddleware)

  // 前端据此决定把对话发到哪条车道；任何一道闸门不过都是 enabled:false，前端回落到 /api/chat。
  routes.get('/config', async (c) => {
    const auth = c.get('auth')
    const gate = await checkGate(c.env, createUserSupabase(c.env, auth.accessToken), auth.userId, deps)
    return c.json({ enabled: gate.ok })
  })

  const gateMiddleware = createMiddleware<LaneBindings>(async (c, next) => {
    const auth = c.get('auth')
    const gate = await checkGate(c.env, createUserSupabase(c.env, auth.accessToken), auth.userId, deps)
    if (!gate.ok) return c.json({ error: gate.error }, gate.status)
    c.set('upstream', gate.upstream)
    await next()
  })

  // 先过闸门再计数：没资格的请求不该消耗额度。
  routes.post('/chat', gateMiddleware, agentLaneRateLimitMiddleware, async (c) => {
    const auth = c.get('auth')
    const body = await c.req.json<{ id?: unknown; messages?: unknown }>().catch(() => null)
    const messages = body?.messages
    if (!Array.isArray(messages) || messages.length === 0) return c.json({ error: 'Missing messages' }, 400)
    if (JSON.stringify(messages).length > MAX_MESSAGES_CHARS) {
      return c.json({ error: '本轮上下文过长，请开启新对话或缩短问题。' }, 413)
    }
    const config = (await deps.loadConfigs(createUserSupabase(c.env, auth.accessToken), auth.userId))[0]
    if (!config) return c.json({ error: '请先在设置页配置 LLM API Key' }, 400)

    const payload = JSON.stringify({ id: body?.id, messages, llm: toServiceLlm(config) })
    let upstream: Response
    try {
      upstream = await c.get('upstream')(auth.userId, payload, c.req.raw.signal)
    } catch {
      return c.json({ error: 'Agent service is unavailable' }, 502)
    }
    if (!upstream.ok) {
      await upstream.body?.cancel()
      return upstream.status === 429
        ? c.json({ error: 'Agent service is busy, please retry shortly' }, 429)
        : c.json({ error: 'Agent service rejected the request' }, 502)
    }
    return streamResponse(upstream)
  })
  return routes
}

export const agentLaneRoutes = createAgentLaneRoutes()
