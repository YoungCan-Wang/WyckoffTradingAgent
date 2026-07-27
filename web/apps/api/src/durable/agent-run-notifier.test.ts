import { describe, expect, it, vi } from 'vitest'
import { AgentRunNotifier } from './agent-run-notifier'

type FakeSocket = { send: (data: string) => void }

function createNotifier(sockets: FakeSocket[] = []) {
  const accepted: WebSocket[] = []
  const ctx = {
    acceptWebSocket: (socket: WebSocket) => accepted.push(socket),
    getWebSockets: () => sockets,
  } as unknown as DurableObjectState
  return { notifier: new AgentRunNotifier(ctx), accepted }
}

describe('AgentRunNotifier', () => {
  it('rejects a connect without a WebSocket upgrade', async () => {
    const { notifier } = createNotifier()
    const response = await notifier.fetch(new Request('https://agent-run-notifier/connect'))
    expect(response.status).toBe(426)
  })

  it('accepts an upgrade through the hibernation API and echoes the subprotocol', async () => {
    const { notifier, accepted } = createNotifier()
    const response = await notifier.fetch(new Request('https://agent-run-notifier/connect', {
      headers: { Upgrade: 'websocket', 'Sec-WebSocket-Protocol': 'bearer, jwt-token' },
    }))
    expect(response.status).toBe(101)
    expect(response.headers.get('Sec-WebSocket-Protocol')).toBe('bearer')
    expect(response.webSocket).toBeTruthy()
    expect(accepted).toHaveLength(1)
  })

  it('broadcasts a notification to every open socket and skips broken ones', async () => {
    const healthy = { send: vi.fn() }
    const broken = { send: vi.fn(() => { throw new Error('socket closed') }) }
    const { notifier } = createNotifier([broken, healthy])

    const response = await notifier.fetch(new Request('https://agent-run-notifier/notify', {
      method: 'POST',
      body: '{"id":"run-1","status":"completed"}',
    }))

    expect(await response.json()).toEqual({ delivered: 1 })
    expect(healthy.send).toHaveBeenCalledWith('{"id":"run-1","status":"completed"}')
  })

  it('returns 404 for unknown paths', async () => {
    const { notifier } = createNotifier()
    const response = await notifier.fetch(new Request('https://agent-run-notifier/other'))
    expect(response.status).toBe(404)
  })
})
