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
  toServiceCredentialsLlm,
  toServiceLlm,
  type AgentUpstream,
  type LaneModelConfig,
  type ServiceCredentials,
} from '../services/agent-lane'
import { loadLLMConfigs, createUserSupabase } from './chat'

type LaneBindings = { Bindings: Env; Variables: { auth: AuthContext; upstream: AgentUpstream } }

type Supabase = ReturnType<typeof createUserSupabase>

export type AgentLaneDeps = {
  isMember: (supabase: Supabase, userId: string) => Promise<boolean>
  loadConfigs: (supabase: Supabase, userId: string) => Promise<LaneModelConfig[]>
  loadCredentials: (supabase: Supabase, userId: string) => Promise<ServiceCredentials>
  resolveUpstream: (env: Env) => AgentUpstream | null
}

// 数据类凭据：和 agent-service-tools.ts 的 loadCredentials 读同一张表、同样取 trim 后的非空值，
// 两条车道各自维护一份是因为取用方式不同（这里是整轮都带上，那边是按工具声明按需取）。
async function loadServiceCredentials(supabase: Supabase, userId: string): Promise<ServiceCredentials> {
  const { data } = await supabase.from('user_settings').select('tickflow_api_key, tushare_token').eq('user_id', userId).single()
  const row = (data || {}) as Record<string, unknown>
  const credentials: ServiceCredentials = {}
  const tushareToken = String(row.tushare_token || '').trim()
  const tickflowKey = String(row.tickflow_api_key || '').trim()
  if (tushareToken) credentials.tushare_token = tushareToken
  if (tickflowKey) credentials.tickflow_api_key = tickflowKey
  return credentials
}

const MAX_MESSAGES_CHARS = 60_000

// 只记事件、请求 ID 和状态码：不记用户 ID、对话内容、模型配置，与沙箱日志的约定一致。
function logLane(requestId: unknown, event: string, detail: Record<string, unknown>): void {
  console.warn(JSON.stringify({ event, timestamp: new Date().toISOString(), requestId, ...detail }))
}

const defaultDeps: AgentLaneDeps = {
  isMember: isActivePlanetMember,
  loadConfigs: (supabase, userId) => loadLLMConfigs(supabase, userId),
  loadCredentials: loadServiceCredentials,
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
    const supabase = createUserSupabase(c.env, auth.accessToken)
    const config = (await deps.loadConfigs(supabase, auth.userId))[0]
    if (!config) return c.json({ error: '请先在设置页配置 LLM API Key' }, 400)
    const llm = toServiceLlm(config)
    const credentials = { ...(await deps.loadCredentials(supabase, auth.userId)), ...toServiceCredentialsLlm(llm) }

    const payload = JSON.stringify({ id: body?.id, messages, llm, credentials })
    let upstream: Response
    try {
      upstream = await c.get('upstream')(auth.userId, payload, c.req.raw.signal)
    } catch (error) {
      logLane(c.get('requestId'), 'agent_lane.upstream_unreachable', { error: error instanceof Error ? error.name : 'unknown' })
      return c.json({ error: 'Agent service is unavailable' }, 502)
    }
    if (!upstream.ok) {
      logLane(c.get('requestId'), 'agent_lane.upstream_rejected', { status: upstream.status })
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
