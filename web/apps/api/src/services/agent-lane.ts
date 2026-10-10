import type { Env } from '../app'

// 会员车道：网关把会员自带的模型配置和对话转给 Agent Runtime 服务，原样回传它的 UI 消息流。
// 这里只放不碰网络的纯逻辑，路由见 routes/agent-lane.ts。

export type ServiceLlm = { provider_name: string; api_key: string; model: string; base_url: string }

export type LaneModelConfig = {
  provider: string
  api_key: string
  model: string
  base_url: string
  protocol?: 'openai' | 'anthropic'
}

export type AgentServiceConfig = { baseUrl: string; token: string }

export type AgentUpstream = (userId: string, body: string, signal: AbortSignal) => Promise<Response>

const SERVICE_ORIGIN_ALLOWED_HTTP_HOSTS = new Set(['127.0.0.1', 'localhost'])
const OFFICIAL_ANTHROPIC_ORIGIN = 'https://api.anthropic.com'

// 前端 TS 侧的 provider 名与 Python 侧不同：anthropic → claude；1route 与自定义的
// OpenAI 兼容端点都走 openai，靠 base_url 区分。
export function toServiceLlm(config: LaneModelConfig): ServiceLlm {
  const base = { api_key: config.api_key, model: config.model }
  if (config.provider === 'gemini') return { ...base, provider_name: 'gemini', base_url: '' }
  if (config.provider === 'anthropic' || config.protocol === 'anthropic') {
    const official = config.base_url.replace(/\/$/, '') === OFFICIAL_ANTHROPIC_ORIGIN
    return { ...base, provider_name: 'claude', base_url: official ? '' : config.base_url }
  }
  if (config.provider === 'deepseek') return { ...base, provider_name: 'deepseek', base_url: config.base_url }
  return { ...base, provider_name: 'openai', base_url: config.base_url }
}

// 服务地址只接受 https；本机开发才放行 http 回环。令牌是网关与服务之间的共享密钥，缺一不可。
export function agentServiceConfig(env: Env): AgentServiceConfig | null {
  const raw = env.AGENT_SERVICE_URL?.trim()
  const token = env.AGENT_SERVICE_TOKEN?.trim()
  if (!raw || !token) return null
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return null
  }
  const loopbackHttp = url.protocol === 'http:' && SERVICE_ORIGIN_ALLOWED_HTTP_HOSTS.has(url.hostname)
  if (url.protocol !== 'https:' && !loopbackHttp) return null
  return { baseUrl: url.origin, token }
}

// 白名单默认为空：没配置就谁都不在里面。`*` 表示所有有效会员，只给开发和全量放开时用。
export function isListedUser(list: string | undefined, userId: string): boolean {
  const entries = (list || '').split(',').map((item) => item.trim()).filter(Boolean)
  return entries.includes('*') || entries.includes(userId)
}

export function isAgentLaneUser(env: Env, userId: string): boolean {
  return isListedUser(env.AGENT_LANE_USERS, userId)
}

export function urlUpstream(config: AgentServiceConfig): AgentUpstream {
  return (userId, body, signal) =>
    fetch(`${config.baseUrl}/v1/ui-turns`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${config.token}`,
        'x-wyckoff-user': userId,
      },
      body,
      signal,
      redirect: 'manual',
    })
}

export function resolveUpstream(env: Env): AgentUpstream | null {
  const config = agentServiceConfig(env)
  return config ? urlUpstream(config) : null
}

// 只放行流式协议需要的头：上游的 Set-Cookie 等一概不透传。
const PASSTHROUGH_HEADERS = ['content-type', 'cache-control', 'x-vercel-ai-ui-message-stream', 'x-accel-buffering']

export function streamResponse(upstream: Response): Response {
  const headers = new Headers()
  for (const name of PASSTHROUGH_HEADERS) {
    const value = upstream.headers.get(name)
    if (value) headers.set(name, value)
  }
  return new Response(upstream.body, { status: upstream.status, headers })
}
