/**
 * 手机遥控电脑上的 agent —— 云端信箱。
 *
 * ## 为什么需要中转
 *
 * agent 跑在用户自己的电脑上（持仓、模型 key、SQLite 都在那台机器）。电脑在 NAT
 * 后面，手机连不到它。所以两边各拉一条 WebSocket 到这里，由 DO 转发。
 *
 * 这不是云端 agent：电脑睡眠就断了。它是遥控器。
 *
 * ## 与 AgentRunNotifier 的区别
 *
 * 那个是**广播**（一份状态推给用户的所有页签）。这个是**双向配对路由**：
 * 手机的请求要送到「那台电脑」，电脑的事件要回到「发起请求的那只手机」。
 * 所以每条连接要带角色（host / remote）和连接 id，转发时按 id 定向。
 *
 * ## 分片键
 *
 * 沿用 `idFromName(userId)` —— 每个账号一个 DO 实例。天然保证同一个人的手机和
 * 电脑落在同一个实例里，而不同账号物理隔离。
 *
 * ## 配对
 *
 * 电脑生成一次性 pairing code（短、能编进二维码），手机扫码后带着它连进来。
 * code 存在 DO 的 SQLite storage 里，有 TTL 且一次性消费 —— 二维码被拍照
 * 也只有那一小段时间的窗口，且已被用掉的 code 无法复用。
 */

const HOST = 'host'
const REMOTE = 'remote'

/** 配对码有效期。够扫码，短到照片泄露的价值有限。 */
const PAIR_TTL_MS = 3 * 60 * 1000

/**
 * 配对成功后发给手机的设备凭证有效期。
 * 断线重连靠它，不再消耗一次性二维码；主人点「全部断开」会清掉。
 */
const DEVICE_GRANT_TTL_MS = 7 * 24 * 60 * 60 * 1000

/** 一个账号最多几台远程设备同时在线。防止 code 泄露后被大量挂连接。 */
const MAX_REMOTES = 8

const DEVICE_GRANTS_KEY = 'device_grants'

type Role = typeof HOST | typeof REMOTE

interface Meta {
  role: Role
  connId: string
  label: string
  since: number
}

interface PairRecord {
  code: string
  expires: number
}

interface DeviceGrant {
  token: string
  expires: number
}

export class RemoteRelay {
  constructor(private readonly ctx: DurableObjectState) {}

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url)
    if (url.pathname === '/pair' && request.method === 'POST') return this.issuePair()
    if (url.pathname === '/devices') return this.devices()
    if (url.pathname === '/revoke' && request.method === 'POST') return this.revoke(await request.text())
    if (url.pathname === '/connect') return this.upgrade(request, url)
    return new Response('Not Found', { status: 404 })
  }

  /** 电脑端调用：发一个一次性配对码，编进二维码。 */
  private async issuePair(): Promise<Response> {
    const code = crypto.randomUUID().replace(/-/g, '').slice(0, 10)
    const record: PairRecord = { code, expires: Date.now() + PAIR_TTL_MS }
    await this.ctx.storage.put('pair', record)
    return Response.json({ code, expires_in_ms: PAIR_TTL_MS })
  }

  /** 当前在线的设备。电脑端设置页用它显示「iPhone - Safari」并提供断开。 */
  private devices(): Response {
    const list = this.ctx.getWebSockets().map((socket) => {
      const meta = this.metaOf(socket)
      return meta ? { conn_id: meta.connId, role: meta.role, label: meta.label, since: meta.since } : null
    })
    return Response.json({ devices: list.filter(Boolean) })
  }

  /** 电脑端踢掉一台设备，或作废未用的配对码。 */
  private async revoke(payload: string): Promise<Response> {
    let connId = ''
    try {
      connId = String((JSON.parse(payload) as { conn_id?: string }).conn_id || '')
    } catch {
      return Response.json({ error: 'bad payload' }, { status: 400 })
    }
    if (connId === '*') {
      // 作废配对码 + 设备凭证 + 踢掉所有 remote。用户点「全部断开」时用。
      await this.ctx.storage.delete('pair')
      await this.ctx.storage.delete(DEVICE_GRANTS_KEY)
      let closed = 0
      for (const socket of this.ctx.getWebSockets()) {
        if (this.metaOf(socket)?.role === REMOTE) {
          this.closeQuietly(socket, 4003, 'revoked')
          closed += 1
        }
      }
      return Response.json({ revoked: closed })
    }
    for (const socket of this.ctx.getWebSockets()) {
      if (this.metaOf(socket)?.connId === connId) {
        this.closeQuietly(socket, 4003, 'revoked')
        return Response.json({ revoked: 1 })
      }
    }
    return Response.json({ revoked: 0 })
  }

  private async upgrade(request: Request, url: URL): Promise<Response> {
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') {
      return new Response('Expected a WebSocket upgrade', { status: 426 })
    }
    const role: Role = url.searchParams.get('role') === HOST ? HOST : REMOTE
    const label = (url.searchParams.get('label') || '').slice(0, 60) || (role === HOST ? '电脑' : '手机')

    let freshGrant: DeviceGrant | null = null
    if (role === REMOTE) {
      // 手机必须出示一次性配对码，或此前配对成功后下发的设备凭证。
      // 仅凭账号 token 不够 —— token 可能在别的未授权设备上。
      const auth = await this.authorizeRemote(
        url.searchParams.get('code') || '',
        url.searchParams.get('device') || '',
      )
      if (!auth.ok) return new Response('Pairing required', { status: 403 })
      freshGrant = auth.freshGrant
      const online = this.ctx.getWebSockets().filter((s) => this.metaOf(s)?.role === REMOTE).length
      if (online >= MAX_REMOTES) return new Response('Too many devices', { status: 429 })
    } else {
      // 一个账号只应有一台电脑在跑 host。新的顶掉旧的 —— 否则用户换机器之后
      // 手机的请求会被路由到那台已经不在用的电脑上，表现为「点了没反应」。
      for (const socket of this.ctx.getWebSockets()) {
        if (this.metaOf(socket)?.role === HOST) this.closeQuietly(socket, 4001, 'replaced')
      }
    }

    const pair = new WebSocketPair()
    const meta: Meta = { role, connId: crypto.randomUUID().slice(0, 8), label, since: Date.now() }
    // Hibernation API：空闲连接不占 DO 时长。serializeAttachment 让 meta 在
    // 休眠后仍然读得到 —— 普通字段会随实例被回收而丢失。
    this.ctx.acceptWebSocket(pair[1])
    pair[1].serializeAttachment(meta)
    if (freshGrant) {
      this.sendQuietly(
        pair[1],
        JSON.stringify({
          type: 'device_grant',
          token: freshGrant.token,
          expires_in_ms: DEVICE_GRANT_TTL_MS,
        }),
      )
    }
    this.broadcastPresence()
    return new Response(null, { status: 101, webSocket: pair[0], headers: acceptedProtocol(request) })
  }

  private async authorizeRemote(
    code: string,
    deviceToken: string,
  ): Promise<{ ok: boolean; freshGrant: DeviceGrant | null }> {
    if (await this.consumePair(code)) {
      return { ok: true, freshGrant: await this.mintDeviceGrant() }
    }
    if (await this.hasValidDeviceGrant(deviceToken)) {
      return { ok: true, freshGrant: null }
    }
    return { ok: false, freshGrant: null }
  }

  private async consumePair(supplied: string): Promise<boolean> {
    if (!supplied) return false
    const record = (await this.ctx.storage.get<PairRecord>('pair')) || null
    if (!record || record.expires < Date.now()) return false
    // 常量时间比较不是重点（code 是随机的、且一次性），但用掉即删是关键：
    // 二维码被拍到也不能重复配对。
    if (record.code !== supplied) return false
    await this.ctx.storage.delete('pair')
    return true
  }

  private async mintDeviceGrant(): Promise<DeviceGrant> {
    const grant: DeviceGrant = {
      token: crypto.randomUUID().replace(/-/g, ''),
      expires: Date.now() + DEVICE_GRANT_TTL_MS,
    }
    const grants = await this.loadDeviceGrants()
    grants.push(grant)
    await this.ctx.storage.put(DEVICE_GRANTS_KEY, grants.slice(-MAX_REMOTES))
    return grant
  }

  private async hasValidDeviceGrant(token: string): Promise<boolean> {
    if (!token) return false
    const now = Date.now()
    const grants = await this.loadDeviceGrants()
    const alive = grants.filter((g) => g.expires >= now)
    if (alive.length !== grants.length) {
      await this.ctx.storage.put(DEVICE_GRANTS_KEY, alive)
    }
    return alive.some((g) => g.token === token)
  }

  private async loadDeviceGrants(): Promise<DeviceGrant[]> {
    const raw = (await this.ctx.storage.get<DeviceGrant[]>(DEVICE_GRANTS_KEY)) || []
    return Array.isArray(raw) ? raw : []
  }

  /**
   * 消息转发。
   *
   * 协议：每条消息是一个 JSON，带 `dir`（up = 手机→电脑，down = 电脑→手机）。
   * 手机发上来的加上 `from`（它的 connId），电脑回下去的按 `to` 定向 ——
   * 否则多台手机在线时，A 的回复会广播给 B。
   */
  webSocketMessage(socket: WebSocket, raw: string | ArrayBuffer): void {
    const meta = this.metaOf(socket)
    if (!meta || typeof raw !== 'string') return

    if (meta.role === REMOTE) {
      const host = this.ctx.getWebSockets().find((s) => this.metaOf(s)?.role === HOST)
      if (!host) {
        // 电脑不在线（睡眠、退出、还没配对）。明确告诉手机，而不是让它一直转圈。
        this.sendQuietly(socket, JSON.stringify({ type: 'host_offline' }))
        return
      }
      this.sendQuietly(host, this.withField(raw, 'from', meta.connId))
      return
    }

    // 来自电脑：按 to 定向回那只手机。
    const target = this.readField(raw, 'to')
    for (const s of this.ctx.getWebSockets()) {
      const m = this.metaOf(s)
      if (m?.role === REMOTE && (!target || m.connId === target)) this.sendQuietly(s, raw)
    }
  }

  webSocketClose(socket: WebSocket): void {
    // 有设备上下线时通知其余连接。电脑端据此更新设备列表，手机端据此显示
    // 「电脑已离线」而不是静默失败。
    this.broadcastPresence(socket)
  }

  webSocketError(socket: WebSocket): void {
    this.broadcastPresence(socket)
  }

  private broadcastPresence(exclude?: WebSocket): void {
    const sockets = this.ctx.getWebSockets().filter((s) => s !== exclude)
    const hostOnline = sockets.some((s) => this.metaOf(s)?.role === HOST)
    const remotes = sockets.filter((s) => this.metaOf(s)?.role === REMOTE).length
    const payload = JSON.stringify({ type: 'presence', host_online: hostOnline, remotes })
    for (const socket of sockets) this.sendQuietly(socket, payload)
  }

  private metaOf(socket: WebSocket): Meta | null {
    try {
      return (socket.deserializeAttachment() as Meta) || null
    } catch {
      return null
    }
  }

  /** 往 JSON 字符串里塞一个字段，不做完整解析再序列化。 */
  private withField(raw: string, key: string, value: string): string {
    try {
      const obj = JSON.parse(raw) as Record<string, unknown>
      obj[key] = value
      return JSON.stringify(obj)
    } catch {
      return raw
    }
  }

  private readField(raw: string, key: string): string {
    try {
      return String((JSON.parse(raw) as Record<string, unknown>)[key] || '')
    } catch {
      return ''
    }
  }

  private sendQuietly(socket: WebSocket, payload: string): void {
    try {
      socket.send(payload)
    } catch {
      // 陈旧连接由运行时回收。
    }
  }

  private closeQuietly(socket: WebSocket, code: number, reason: string): void {
    try {
      socket.close(code, reason)
    } catch {
      // 已经关掉了。
    }
  }
}

// 浏览器要求服务端回显请求的 subprotocol，否则握手被中止。
function acceptedProtocol(request: Request): Record<string, string> {
  const requested = request.headers.get('Sec-WebSocket-Protocol')?.split(',')[0]?.trim()
  return requested ? { 'Sec-WebSocket-Protocol': requested } : {}
}
