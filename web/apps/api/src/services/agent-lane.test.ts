import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  agentServiceConfig,
  isAgentLaneUser,
  streamResponse,
  toServiceLlm,
  urlUpstream,
  type LaneModelConfig,
} from './agent-lane'

const base = { api_key: 'sk-test', model: 'm1' }

describe('toServiceLlm', () => {
  const cases: Array<[string, LaneModelConfig, Record<string, string>]> = [
    ['deepseek keeps its endpoint', { ...base, provider: 'deepseek', base_url: 'https://api.deepseek.com/v1' }, { provider_name: 'deepseek', base_url: 'https://api.deepseek.com/v1' }],
    ['openai keeps its endpoint', { ...base, provider: 'openai', base_url: 'https://api.openai.com/v1' }, { provider_name: 'openai', base_url: 'https://api.openai.com/v1' }],
    ['1route is an OpenAI-compatible endpoint', { ...base, provider: '1route', base_url: 'https://api.1route.dev/v1' }, { provider_name: 'openai', base_url: 'https://api.1route.dev/v1' }],
    ['custom providers are OpenAI-compatible unless they say otherwise', { ...base, provider: 'my-gw', base_url: 'https://gw.example/v1' }, { provider_name: 'openai', base_url: 'https://gw.example/v1' }],
    ['official anthropic drops the base url', { ...base, provider: 'anthropic', protocol: 'anthropic', base_url: 'https://api.anthropic.com' }, { provider_name: 'claude', base_url: '' }],
    ['a proxied anthropic endpoint keeps it', { ...base, provider: 'anthropic', protocol: 'anthropic', base_url: 'https://proxy.example' }, { provider_name: 'claude', base_url: 'https://proxy.example' }],
    ['a custom provider speaking the anthropic protocol is claude', { ...base, provider: 'my-gw', protocol: 'anthropic', base_url: 'https://gw.example' }, { provider_name: 'claude', base_url: 'https://gw.example' }],
    ['gemini has no base url on the python side', { ...base, provider: 'gemini', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai' }, { provider_name: 'gemini', base_url: '' }],
  ]

  it.each(cases)('%s', (_name, config, expected) => {
    expect(toServiceLlm(config)).toEqual({ ...base, ...expected })
  })
})

describe('agentServiceConfig', () => {
  it('needs both a url and a token', () => {
    expect(agentServiceConfig({})).toBeNull()
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'https://agent.example' })).toBeNull()
    expect(agentServiceConfig({ AGENT_SERVICE_TOKEN: 'secret' })).toBeNull()
  })

  it('accepts https and normalises the origin', () => {
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'https://agent.example/some/path/', AGENT_SERVICE_TOKEN: ' secret ' }))
      .toEqual({ baseUrl: 'https://agent.example', token: 'secret' })
  })

  it('allows plain http only on loopback', () => {
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'http://127.0.0.1:18080', AGENT_SERVICE_TOKEN: 't' })).not.toBeNull()
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'http://localhost:18080', AGENT_SERVICE_TOKEN: 't' })).not.toBeNull()
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'http://agent.example', AGENT_SERVICE_TOKEN: 't' })).toBeNull()
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'ftp://agent.example', AGENT_SERVICE_TOKEN: 't' })).toBeNull()
  })

  it('rejects an unparsable url', () => {
    expect(agentServiceConfig({ AGENT_SERVICE_URL: 'not a url', AGENT_SERVICE_TOKEN: 't' })).toBeNull()
  })
})

describe('isAgentLaneUser', () => {
  it('is closed by default', () => {
    expect(isAgentLaneUser({}, 'u1')).toBe(false)
    expect(isAgentLaneUser({ AGENT_LANE_USERS: '  ' }, 'u1')).toBe(false)
  })

  it('matches listed user ids exactly', () => {
    const env = { AGENT_LANE_USERS: 'u1, u2' }
    expect(isAgentLaneUser(env, 'u1')).toBe(true)
    expect(isAgentLaneUser(env, 'u2')).toBe(true)
    expect(isAgentLaneUser(env, 'u')).toBe(false)
    expect(isAgentLaneUser(env, 'u12')).toBe(false)
  })

  it('treats * as every user', () => {
    expect(isAgentLaneUser({ AGENT_LANE_USERS: '*' }, 'anyone')).toBe(true)
  })
})

describe('urlUpstream', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts the body to the ui-turns endpoint with the gateway credentials and never follows redirects', async () => {
    const fetchMock = vi.fn(async () => new Response('ok'))
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal

    await urlUpstream({ baseUrl: 'https://agent.example', token: 'secret' })('user-7', '{"a":1}', signal)

    expect(fetchMock).toHaveBeenCalledWith('https://agent.example/v1/ui-turns', {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer secret', 'x-wyckoff-user': 'user-7' },
      body: '{"a":1}',
      signal,
      redirect: 'manual',
    })
  })
})

describe('streamResponse', () => {
  it('forwards only the headers the stream protocol needs', async () => {
    const upstream = new Response('data: [DONE]\n\n', {
      status: 200,
      headers: {
        'content-type': 'text/event-stream',
        'x-vercel-ai-ui-message-stream': 'v1',
        'cache-control': 'no-store',
        'set-cookie': 'session=1',
        'x-internal': 'leak',
      },
    })

    const response = streamResponse(upstream)

    expect(response.headers.get('x-vercel-ai-ui-message-stream')).toBe('v1')
    expect(response.headers.get('content-type')).toBe('text/event-stream')
    expect(response.headers.get('set-cookie')).toBeNull()
    expect(response.headers.get('x-internal')).toBeNull()
    expect(await response.text()).toBe('data: [DONE]\n\n')
  })
})
