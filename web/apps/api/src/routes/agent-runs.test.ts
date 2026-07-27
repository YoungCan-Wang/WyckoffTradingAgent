import { describe, expect, it } from 'vitest'
import { agentRunRoutes, parseAgentRunInput, websocketBearerToken } from './agent-runs'

describe('Agent run input', () => {
  it('accepts the single research task supported by the MVP', () => {
    expect(parseAgentRunInput({ kind: 'python_research', script: 'print(42)' })).toEqual({
      data: { kind: 'python_research', script: 'print(42)' },
    })
  })

  it('rejects arbitrary task kinds and oversized scripts', () => {
    expect(parseAgentRunInput({ kind: 'shell', script: 'ls' })).toHaveProperty('error')
    expect(parseAgentRunInput({ kind: 'python_research', script: 'x'.repeat(12_001) })).toHaveProperty('error')
  })
})

describe('Agent run websocket endpoint', () => {
  it('extracts the access token from the bearer subprotocol', () => {
    expect(websocketBearerToken('bearer, jwt-token')).toBe('jwt-token')
    expect(websocketBearerToken('bearer')).toBeNull()
    expect(websocketBearerToken('basic, jwt-token')).toBeNull()
    expect(websocketBearerToken(undefined)).toBeNull()
  })

  it('rejects plain HTTP requests before authenticating', async () => {
    const response = await agentRunRoutes.request('/ws', {}, {})
    expect(response.status).toBe(426)
  })

  it('reports unavailability when the notifier binding is missing', async () => {
    const response = await agentRunRoutes.request('/ws', { headers: { Upgrade: 'websocket' } }, {})
    expect(response.status).toBe(503)
  })

  it('rejects an upgrade without a bearer token before touching the notifier', async () => {
    const response = await agentRunRoutes.request(
      '/ws',
      { headers: { Upgrade: 'websocket' } },
      { AGENT_RUN_NOTIFIER: {} as DurableObjectNamespace },
    )
    expect(response.status).toBe(401)
  })
})
