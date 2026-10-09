import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentUpstream, LaneModelConfig } from '../services/agent-lane'
import { createAgentLaneRoutes, type AgentLaneDeps } from './agent-lane'

const limiter = vi.hoisted(() => ({ calls: 0 }))

vi.mock('../middleware/auth', () => ({
  authMiddleware: async (c: { set: (key: string, value: unknown) => void }, next: () => Promise<void>) => {
    c.set('auth', { userId: 'user-1', accessToken: 'token-1' })
    await next()
  },
}))
vi.mock('../middleware/rate-limit', () => ({
  // 普通函数而不是 vi.fn：Hono 对 vi.fn 包装过的中间件不会传 next。
  agentLaneRateLimitMiddleware: async (_c: unknown, next: () => Promise<void>) => {
    limiter.calls += 1
    await next()
  },
}))
vi.mock('./chat', () => ({ loadLLMConfigs: vi.fn(), createUserSupabase: vi.fn(() => ({})) }))
vi.mock('../middleware/planet-membership', () => ({ isActivePlanetMember: vi.fn() }))

const CONFIG: LaneModelConfig = { provider: 'deepseek', api_key: 'sk-secret', model: 'deepseek-v4-flash', base_url: 'https://api.deepseek.com/v1' }
const MESSAGES = [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: '威科夫量价' }] }]
const ENV = { AGENT_LANE_USERS: 'user-1' }

function uiStream(): Response {
  return new Response('data: {"type":"start"}\n\ndata: [DONE]\n\n', {
    headers: { 'content-type': 'text/event-stream', 'x-vercel-ai-ui-message-stream': 'v1', 'set-cookie': 'x=1' },
  })
}

function build(overrides: Partial<AgentLaneDeps> = {}) {
  const upstream = vi.fn<AgentUpstream>(async () => uiStream())
  const deps: Partial<AgentLaneDeps> = {
    isMember: async () => true,
    loadConfigs: async () => [CONFIG],
    resolveUpstream: () => upstream,
    ...overrides,
  }
  return { routes: createAgentLaneRoutes(deps), upstream }
}

function post(routes: ReturnType<typeof createAgentLaneRoutes>, body: unknown, env: Record<string, string> = ENV) {
  return routes.request('/chat', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, env)
}

beforeEach(() => { limiter.calls = 0 })

describe('GET /config', () => {
  it('is enabled only when the service is configured, the user is listed and a member', async () => {
    const { routes } = build()
    expect(await (await routes.request('/config', {}, ENV)).json()).toEqual({ enabled: true })
  })

  it.each([
    ['the service is not configured', { resolveUpstream: () => null }, ENV],
    ['the user is not listed', {}, { AGENT_LANE_USERS: 'someone-else' }],
    ['the user is not an active member', { isMember: async () => false }, ENV],
  ] as const)('is disabled when %s', async (_name, overrides, env) => {
    const { routes } = build(overrides)
    expect(await (await routes.request('/config', {}, env)).json()).toEqual({ enabled: false })
  })
})

describe('POST /chat gates', () => {
  it('answers 503 when no service is configured', async () => {
    const { routes } = build({ resolveUpstream: () => null })
    expect((await post(routes, { messages: MESSAGES })).status).toBe(503)
  })

  it('answers 403 for users outside the allowlist', async () => {
    const { routes, upstream } = build()
    expect((await post(routes, { messages: MESSAGES }, { AGENT_LANE_USERS: 'other' })).status).toBe(403)
    expect(upstream).not.toHaveBeenCalled()
  })

  it('answers 403 for non-members', async () => {
    const { routes, upstream } = build({ isMember: async () => false })
    expect((await post(routes, { messages: MESSAGES })).status).toBe(403)
    expect(upstream).not.toHaveBeenCalled()
  })

  it('does not spend rate-limit budget on requests that fail a gate', async () => {
    const { routes } = build({ isMember: async () => false })
    await post(routes, { messages: MESSAGES })
    expect(limiter.calls).toBe(0)
  })
})

describe('POST /chat rate limit', () => {
  it('spends budget once a request has passed the gates', async () => {
    const { routes } = build()
    await post(routes, { messages: MESSAGES })
    expect(limiter.calls).toBe(1)
  })
})

describe('POST /chat validation', () => {
  it('rejects a body without messages', async () => {
    const { routes } = build()
    expect((await post(routes, {})).status).toBe(400)
    expect((await post(routes, { messages: [] })).status).toBe(400)
  })

  it('rejects an oversized conversation', async () => {
    const { routes, upstream } = build()
    const big = [{ id: 'm', role: 'user', parts: [{ type: 'text', text: 'x'.repeat(61_000) }] }]
    expect((await post(routes, { messages: big })).status).toBe(413)
    expect(upstream).not.toHaveBeenCalled()
  })

  it('asks the user to configure a model when none is saved', async () => {
    const { routes, upstream } = build({ loadConfigs: async () => [] })
    const response = await post(routes, { messages: MESSAGES })
    expect(response.status).toBe(400)
    expect(upstream).not.toHaveBeenCalled()
  })
})

describe('POST /chat forwarding', () => {
  it('forwards the conversation with the mapped model config and streams the reply back untouched', async () => {
    const { routes, upstream } = build()

    const response = await post(routes, { id: 'chat-1', messages: MESSAGES, watchlist: [{ code: '600519' }] })

    expect(response.status).toBe(200)
    expect(response.headers.get('x-vercel-ai-ui-message-stream')).toBe('v1')
    expect(response.headers.get('set-cookie')).toBeNull()
    expect(await response.text()).toBe('data: {"type":"start"}\n\ndata: [DONE]\n\n')

    const [userId, payload, signal] = upstream.mock.calls[0] as [string, string, AbortSignal]
    expect(userId).toBe('user-1')
    expect(signal).toBeInstanceOf(AbortSignal)
    expect(JSON.parse(payload)).toEqual({
      id: 'chat-1',
      messages: MESSAGES,
      llm: { provider_name: 'deepseek', api_key: 'sk-secret', model: 'deepseek-v4-flash', base_url: 'https://api.deepseek.com/v1' },
    })
  })

  it('does not pass browser-only fields on to the service', async () => {
    const { routes, upstream } = build()
    await post(routes, { id: 'chat-1', messages: MESSAGES, watchlist: [{ code: '600519' }], marketWatch: { quotes: [] } })
    const payload = JSON.parse((upstream.mock.calls[0] as unknown as [string, string])[1]) as Record<string, unknown>
    expect(Object.keys(payload).sort()).toEqual(['id', 'llm', 'messages'])
  })
})

describe('POST /chat upstream failures', () => {
  it('answers 502 when the service cannot be reached, without echoing the key', async () => {
    const { routes } = build({ resolveUpstream: () => async () => { throw new Error('connect ECONNREFUSED sk-secret') } })
    const response = await post(routes, { messages: MESSAGES })
    expect(response.status).toBe(502)
    expect(await response.text()).not.toContain('sk-secret')
  })

  it('maps a busy service to 429', async () => {
    const { routes } = build({ resolveUpstream: () => async () => new Response('{"error":"a turn is already running"}', { status: 429 }) })
    expect((await post(routes, { messages: MESSAGES })).status).toBe(429)
  })

  it('logs why the service said no, without user ids, content or keys', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { routes } = build({ resolveUpstream: () => async () => new Response('internal detail sk-secret', { status: 409 }) })

    await post(routes, { messages: MESSAGES })

    const line = String(warn.mock.calls[0]?.[0])
    expect(JSON.parse(line)).toMatchObject({ event: 'agent_lane.upstream_rejected', status: 409 })
    for (const forbidden of ['sk-secret', 'user-1', '威科夫量价', 'internal detail']) expect(line).not.toContain(forbidden)
  })

  it('logs an unreachable service by error name only', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { routes } = build({ resolveUpstream: () => async () => { throw new TypeError('connect ECONNREFUSED sk-secret') } })

    await post(routes, { messages: MESSAGES })

    const line = String(warn.mock.calls[0]?.[0])
    expect(JSON.parse(line)).toMatchObject({ event: 'agent_lane.upstream_unreachable', error: 'TypeError' })
    expect(line).not.toContain('sk-secret')
  })

  it.each([400, 401, 409, 500])('maps a %i from the service to 502 and does not leak its body', async (status) => {
    const { routes } = build({ resolveUpstream: () => async () => new Response('internal detail sk-secret', { status }) })
    const response = await post(routes, { messages: MESSAGES })
    expect(response.status).toBe(502)
    expect(await response.text()).not.toContain('internal detail')
  })
})
