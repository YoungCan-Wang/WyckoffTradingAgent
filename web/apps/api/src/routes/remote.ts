import { Hono } from 'hono'
import type { Env } from '../app'
import { authMiddleware, createUserSupabase, resolveUserId, type AuthContext } from '../middleware/auth'
import { isActiveWhitelistUser, whitelistMiddleware } from '../middleware/whitelist'
import { websocketBearerToken } from './agent-runs'

type RemoteBindings = { Bindings: Env; Variables: { auth: AuthContext } }

export const remoteRoutes = new Hono<RemoteBindings>()

/**
 * 手机遥控的信箱入口。
 *
 * 每个账号一个 Durable Object 实例（`idFromName(userId)`）—— 天然保证同一个人的
 * 手机和电脑落在同一个实例，不同账号物理隔离。这个分片键沿用 agent-runs 的做法。
 *
 * 鉴权手工做且**必须注册在共享中间件之前**：浏览器无法给 WebSocket upgrade 带
 * Authorization header，token 只能走 `Sec-WebSocket-Protocol: "bearer, <jwt>"`。
 * 原因和 agent-runs.ts:19 那条注释相同，`websocketBearerToken` 也直接复用。
 */
remoteRoutes.get('/ws', async (c) => {
  if (c.req.header('Upgrade')?.toLowerCase() !== 'websocket') {
    return c.json({ error: 'Expected a WebSocket upgrade' }, 426)
  }
  const namespace = c.env.REMOTE_RELAY
  if (!namespace) return c.json({ error: 'Remote control is unavailable' }, 503)
  const token = websocketBearerToken(c.req.header('Sec-WebSocket-Protocol'))
  if (!token) return c.json({ error: 'Unauthorized' }, 401)
  const userId = await resolveUserId(c.env, token)
  if (!userId) return c.json({ error: 'Invalid token' }, 401)
  if (!(await isActiveWhitelistUser(createUserSupabase(c.env, token), userId))) {
    return c.json({ error: 'Whitelist required' }, 403)
  }
  // role / label / code 走 query，转交时原样带上 —— DO 负责校验配对码。
  const url = new URL(c.req.url)
  const target = new URL('https://remote-relay/connect')
  target.search = url.search
  const stub = namespace.get(namespace.idFromName(userId))
  return stub.fetch(new Request(target, c.req.raw))
})

// 以下是普通 HTTP，走标准中间件。
remoteRoutes.use('*', authMiddleware)
remoteRoutes.use('*', whitelistMiddleware)

/** 电脑端要一个一次性配对码，编进二维码。 */
remoteRoutes.post('/pair', async (c) => {
  const namespace = c.env.REMOTE_RELAY
  if (!namespace) return c.json({ error: 'Remote control is unavailable' }, 503)
  const stub = namespace.get(namespace.idFromName(c.get('auth').userId))
  const res = await stub.fetch(new Request('https://remote-relay/pair', { method: 'POST' }))
  return new Response(res.body, res)
})

/** 当前在线设备。电脑端设置页显示并提供断开。 */
remoteRoutes.get('/devices', async (c) => {
  const namespace = c.env.REMOTE_RELAY
  if (!namespace) return c.json({ error: 'Remote control is unavailable' }, 503)
  const stub = namespace.get(namespace.idFromName(c.get('auth').userId))
  const res = await stub.fetch(new Request('https://remote-relay/devices'))
  return new Response(res.body, res)
})

/** 踢掉一台设备（conn_id），或传 "*" 作废配对码并断开全部远程。 */
remoteRoutes.post('/revoke', async (c) => {
  const namespace = c.env.REMOTE_RELAY
  if (!namespace) return c.json({ error: 'Remote control is unavailable' }, 503)
  const body = await c.req.text()
  const stub = namespace.get(namespace.idFromName(c.get('auth').userId))
  const res = await stub.fetch(new Request('https://remote-relay/revoke', { method: 'POST', body }))
  return new Response(res.body, res)
})
