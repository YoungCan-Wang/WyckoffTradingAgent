import { describe, expect, it, vi } from 'vitest'
import { RemoteRelay } from './remote-relay'

/**
 * 手搓 fake ctx —— 和 agent-run-notifier.test.ts 同样的路子。
 * vitest.config.ts 的 miniflare bindings 没声明 DO，所以走不了 runInDurableObject。
 */
type FakeSocket = {
  send: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
  attachment: unknown
  serializeAttachment: (value: unknown) => void
  deserializeAttachment: () => unknown
}

function socket(meta?: unknown): FakeSocket {
  const s: FakeSocket = {
    send: vi.fn(),
    close: vi.fn(),
    attachment: meta,
    serializeAttachment: (value: unknown) => { s.attachment = value },
    deserializeAttachment: () => s.attachment,
  }
  return s
}

function createRelay(sockets: FakeSocket[] = []) {
  const store = new Map<string, unknown>()
  const live = [...sockets]
  const ctx = {
    acceptWebSocket: (s: WebSocket) => live.push(s as unknown as FakeSocket),
    getWebSockets: () => live,
    storage: {
      get: async (key: string) => store.get(key),
      put: async (key: string, value: unknown) => { store.set(key, value) },
      delete: async (key: string) => { store.delete(key) },
    },
  } as unknown as DurableObjectState
  return { relay: new RemoteRelay(ctx), store, live }
}

const upgrade = (query = '') =>
  new Request(`https://remote-relay/connect${query}`, {
    headers: { Upgrade: 'websocket', 'Sec-WebSocket-Protocol': 'bearer, jwt' },
  })

const host = () => socket({ role: 'host', connId: 'h1', label: '电脑', since: 1 })
const remote = (connId = 'r1') => socket({ role: 'remote', connId, label: '手机', since: 2 })

describe('RemoteRelay 配对', () => {
  it('发一个带过期时间的一次性配对码', async () => {
    const { relay, store } = createRelay()
    const res = await relay.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))
    const body = await res.json() as { code: string; expires_in_ms: number }
    expect(body.code).toHaveLength(10)
    expect(body.expires_in_ms).toBeGreaterThan(0)
    expect(store.get('pair')).toBeTruthy()
  })

  it('手机没有配对码时拒绝连接', async () => {
    // 光有账号 token 不够 —— token 可能在别的设备上。配对码是「主人当场授权过
    // 这台设备」的证据。
    const { relay } = createRelay()
    const res = await relay.fetch(upgrade('?role=remote'))
    expect(res.status).toBe(403)
  })

  it('配对码错误时拒绝', async () => {
    const { relay } = createRelay()
    await relay.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))
    const res = await relay.fetch(upgrade('?role=remote&code=wrongcode1'))
    expect(res.status).toBe(403)
  })

  it('配对码用掉即失效', async () => {
    // 二维码被拍照也不能重复配对。
    const { relay } = createRelay()
    const issued = await relay.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))
    const { code } = await issued.json() as { code: string }

    const first = await relay.fetch(upgrade(`?role=remote&code=${code}`))
    expect(first.status).toBe(101)

    const second = await relay.fetch(upgrade(`?role=remote&code=${code}`))
    expect(second.status).toBe(403)
  })

  it('过期的配对码无效', async () => {
    const { relay, store } = createRelay()
    store.set('pair', { code: 'expiredcod', expires: Date.now() - 1000 })
    const res = await relay.fetch(upgrade('?role=remote&code=expiredcod'))
    expect(res.status).toBe(403)
  })

  it('电脑端连接不需要配对码', async () => {
    // 电脑就是签发方，它已经通过账号鉴权了。
    const { relay } = createRelay()
    const res = await relay.fetch(upgrade('?role=host'))
    expect(res.status).toBe(101)
  })

  it('限制同时在线的远程设备数', async () => {
    const many = Array.from({ length: 8 }, (_, i) => remote(`r${i}`))
    const { relay } = createRelay(many)
    await relay.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))
    const { code } = await (await relay.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))).json() as { code: string }
    const res = await relay.fetch(upgrade(`?role=remote&code=${code}`))
    expect(res.status).toBe(429)
  })
})

describe('RemoteRelay 转发', () => {
  it('把手机的请求送给电脑，并标上来源', async () => {
    // 不标 from 的话，多台手机在线时电脑不知道该回给谁。
    const h = host()
    const r = remote('phone-a')
    const { relay } = createRelay([h, r])
    relay.webSocketMessage(r as unknown as WebSocket, '{"id":1,"method":"portfolio"}')
    expect(h.send).toHaveBeenCalledOnce()
    expect(JSON.parse(h.send.mock.calls[0][0])).toMatchObject({ method: 'portfolio', from: 'phone-a' })
  })

  it('电脑的回复按 to 定向，不广播给其他手机', async () => {
    const h = host()
    const a = remote('phone-a')
    const b = remote('phone-b')
    const { relay } = createRelay([h, a, b])
    relay.webSocketMessage(h as unknown as WebSocket, '{"to":"phone-a","type":"text_delta"}')
    expect(a.send).toHaveBeenCalledOnce()
    expect(b.send).not.toHaveBeenCalled()
  })

  it('电脑不在线时明确告知手机', async () => {
    // 否则手机一直转圈，用户不知道是网慢还是电脑睡了。
    const r = remote()
    const { relay } = createRelay([r])
    relay.webSocketMessage(r as unknown as WebSocket, '{"id":1,"method":"portfolio"}')
    expect(JSON.parse(r.send.mock.calls[0][0])).toEqual({ type: 'host_offline' })
  })

  it('新电脑顶掉旧电脑', async () => {
    // 用户换机器后，手机的请求不该被路由到已经不用的那台。
    const old = host()
    const { relay } = createRelay([old])
    await relay.fetch(upgrade('?role=host'))
    expect(old.close).toHaveBeenCalledWith(4001, 'replaced')
  })

  it('坏 JSON 原样转发而不是崩掉', async () => {
    const h = host()
    const r = remote()
    const { relay } = createRelay([h, r])
    relay.webSocketMessage(r as unknown as WebSocket, 'not json')
    expect(h.send).toHaveBeenCalledWith('not json')
  })

  it('忽略二进制帧', async () => {
    const h = host()
    const r = remote()
    const { relay } = createRelay([h, r])
    relay.webSocketMessage(r as unknown as WebSocket, new ArrayBuffer(8))
    expect(h.send).not.toHaveBeenCalled()
  })
})

describe('RemoteRelay 设备管理', () => {
  it('列出在线设备', async () => {
    const { relay } = createRelay([host(), remote('phone-a')])
    const res = await relay.fetch(new Request('https://remote-relay/devices'))
    const body = await res.json() as { devices: Array<{ role: string; conn_id: string }> }
    expect(body.devices.map((d) => d.role)).toEqual(['host', 'remote'])
  })

  it('按 conn_id 踢掉一台设备', async () => {
    const a = remote('phone-a')
    const b = remote('phone-b')
    const { relay } = createRelay([a, b])
    const res = await relay.fetch(new Request('https://remote-relay/revoke', {
      method: 'POST', body: '{"conn_id":"phone-a"}',
    }))
    expect(await res.json()).toEqual({ revoked: 1 })
    expect(a.close).toHaveBeenCalledWith(4003, 'revoked')
    expect(b.close).not.toHaveBeenCalled()
  })

  it('通配符断开所有远程设备并作废配对码', async () => {
    const h = host()
    const a = remote('phone-a')
    const { relay, store } = createRelay([h, a])
    await relay.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))
    const res = await relay.fetch(new Request('https://remote-relay/revoke', {
      method: 'POST', body: '{"conn_id":"*"}',
    }))
    expect(await res.json()).toEqual({ revoked: 1 })
    expect(a.close).toHaveBeenCalled()
    expect(h.close).not.toHaveBeenCalled()  // 电脑自己不该被踢
    expect(store.get('pair')).toBeUndefined()
  })

  it('踢不存在的设备不报错', async () => {
    const { relay } = createRelay([remote('phone-a')])
    const res = await relay.fetch(new Request('https://remote-relay/revoke', {
      method: 'POST', body: '{"conn_id":"nope"}',
    }))
    expect(await res.json()).toEqual({ revoked: 0 })
  })

  it('坏 payload 返回 400', async () => {
    const { relay } = createRelay()
    const res = await relay.fetch(new Request('https://remote-relay/revoke', { method: 'POST', body: 'x' }))
    expect(res.status).toBe(400)
  })
})

describe('RemoteRelay 在线状态', () => {
  it('设备断开时通知其余连接', async () => {
    const h = host()
    const a = remote('phone-a')
    const b = remote('phone-b')
    const { relay } = createRelay([h, a, b])
    relay.webSocketClose(b as unknown as WebSocket)
    // 断开的那个不再收，其余两个收到 presence
    expect(b.send).not.toHaveBeenCalled()
    expect(JSON.parse(h.send.mock.calls[0][0])).toMatchObject({ type: 'presence', host_online: true })
  })

  it('电脑掉线后 presence 反映出来', async () => {
    const h = host()
    const a = remote('phone-a')
    const { relay } = createRelay([h, a])
    relay.webSocketClose(h as unknown as WebSocket)
    expect(JSON.parse(a.send.mock.calls[0][0])).toMatchObject({ host_online: false })
  })
})

describe('RemoteRelay 其他', () => {
  it('非 upgrade 请求返回 426', async () => {
    const { relay } = createRelay()
    const res = await relay.fetch(new Request('https://remote-relay/connect'))
    expect(res.status).toBe(426)
  })

  it('未知路径返回 404', async () => {
    const { relay } = createRelay()
    expect((await relay.fetch(new Request('https://remote-relay/nope'))).status).toBe(404)
  })

  it('回显 subprotocol，否则浏览器中止握手', async () => {
    const { relay } = createRelay()
    const res = await relay.fetch(upgrade('?role=host'))
    expect(res.headers.get('Sec-WebSocket-Protocol')).toBe('bearer')
  })
})
