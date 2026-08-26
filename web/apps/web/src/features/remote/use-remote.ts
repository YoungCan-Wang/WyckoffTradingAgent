/**
 * 手机端到电脑的连接。
 *
 * 手机不直接跑 agent —— agent 在用户的电脑上（持仓、模型 key、SQLite 都在那）。
 * 这个 hook 维持一条到云端信箱的 WebSocket，把方法调用发过去、把事件流收回来。
 *
 * 协议与桌面端完全一致（`cli/ipc/methods.py` 的 63 个方法），所以手机上能做的事
 * 和电脑上一模一样：对话、改持仓、设止损、审批。
 */
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import { supabase } from '@/lib/supabase'
import { apiUrl } from '@/lib/api-url'

export interface RemoteEvent {
  id?: number | string
  type: string
  [key: string]: unknown
}

type Waiter = {
  onEvent: (event: RemoteEvent) => void
  resolve: () => void
  reject: (reason: Error) => void
}

export type LinkState = 'connecting' | 'paired' | 'host_offline' | 'unauthorized' | 'failed'

/** 配对码从 URL hash 里来（电脑生成的二维码带着它）。 */
export function pairingCodeFromHash(hash: string): string {
  const match = /(?:^|[#&])code=([0-9a-z]+)/i.exec(hash || '')
  // noUncheckedIndexedAccess 下捕获组是 string | undefined，即便正则保证它存在。
  return match?.[1] ?? ''
}

/** 建连所需的一切可变状态。从 useRemote 里抽出来，好让 wiring 逻辑离开 hook。 */
interface SocketDeps {
  socket: MutableRefObject<WebSocket | null>
  waiters: MutableRefObject<Map<number, Waiter>>
  attempt: MutableRefObject<number>
  closed: MutableRefObject<boolean>
  setState: (next: LinkState) => void
  reconnect: () => void
}

/**
 * 解析一条下行消息，派发给状态或对应的调用方。
 *
 * 从 useRemote 里抽出来的：那个 hook 原本 112 行，超过 90 的上限。
 * 这段是最独立的一块 —— 它只依赖 waiters 和 setState，不碰重连逻辑。
 */
function handleMessage (raw: string, deps: SocketDeps): void {
  let payload: RemoteEvent
  try {
    payload = JSON.parse(raw)
  } catch {
    return
  }
  if (payload.type === 'presence') {
    deps.setState(payload.host_online ? 'paired' : 'host_offline')
    return
  }
  if (payload.type === 'host_offline') { deps.setState('host_offline'); return }

  const waiter = deps.waiters.current.get(Number(payload.id))
  if (!waiter) return
  if (payload.type === 'end') {
    deps.waiters.current.delete(Number(payload.id))
    waiter.resolve()
    return
  }
  waiter.onEvent(payload)
}

/** 断线处理：区分「被踢」与「网络抖动」，后者退避重连。 */
function handleClose (evt: CloseEvent, deps: SocketDeps): void {
  deps.socket.current = null
  // 4003 = 被电脑端踢掉。那是用户主动的决定，不该自动爬回来。
  if (evt.code === 4003) { deps.closed.current = true; deps.setState('unauthorized'); return }
  if (deps.closed.current) return
  // 挂起的调用要收到失败，否则界面永久转圈。
  for (const waiter of deps.waiters.current.values()) waiter.reject(new Error('连接断开'))
  deps.waiters.current.clear()
  deps.setState('connecting')
  const delay = Math.min(1000 * 2 ** deps.attempt.current, 15000)
  deps.attempt.current += 1
  setTimeout(deps.reconnect, delay)
}

/** 拿 token 和配对码，拼出 WS 地址。返回 null 表示不具备连接条件。 */
async function resolveSocketUrl (setState: (n: LinkState) => void): Promise<{ url: string; token: string; code: string } | null> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  if (!token) { setState('unauthorized'); return null }

  // 配对码只在第一次连接时用（信箱那边一次性消费）。存进 sessionStorage 是为了
  // 熬过重连 —— 但重连时不再带它，否则第二次必然因为「码已用掉」被拒。
  const code = pairingCodeFromHash(window.location.hash)
  if (code) sessionStorage.setItem('wyckoff.paired', '1')
  const paired = sessionStorage.getItem('wyckoff.paired') === '1'
  if (!code && !paired) { setState('unauthorized'); return null }

  // 复用 apiUrl 而不是自己拼 base：它已经处理了 dev/prod 与 VITE_API_URL 覆盖。
  const label = encodeURIComponent(navigator.userAgent.includes('iPhone') ? 'iPhone' : '手机')
  const http = apiUrl(`/api/remote/ws?role=remote&label=${label}${code ? `&code=${code}` : ''}`)
  return { url: http.replace(/^http/, 'ws'), token, code }
}

export function useRemote () {
  const [state, setState] = useState<LinkState>('connecting')
  const socket = useRef<WebSocket | null>(null)
  const waiters = useRef(new Map<number, Waiter>())
  const nextId = useRef(1)
  // 重连计数放 ref：它只影响下一次退避时长，不该触发重渲染。
  const attempt = useRef(0)
  const closed = useRef(false)

  const connect = useCallback(async () => {
    if (closed.current) return
    const resolved = await resolveSocketUrl(setState)
    if (!resolved) return

    // token 走 subprotocol：浏览器不能给 WS upgrade 带 Authorization header。
    // 这个格式和 agent-run-socket 那边一致，云端按同一套解析。
    const ws = new WebSocket(resolved.url, ['bearer', resolved.token])
    socket.current = ws
    const deps: SocketDeps = {
      socket, waiters, attempt, closed, setState, reconnect: () => void connect()
    }

    ws.onopen = () => {
      attempt.current = 0
      setState('paired')
      // 配对码用掉了就从地址栏抹掉 —— 留在 history 里等于把它写进浏览器记录。
      if (resolved.code) history.replaceState(null, '', window.location.pathname)
    }
    ws.onmessage = (evt) => handleMessage(String(evt.data), deps)
    ws.onclose = (evt) => handleClose(evt, deps)
    ws.onerror = () => {
      if (attempt.current > 3) setState('failed')
    }
  }, [])

  useEffect(() => {
    closed.current = false
    void connect()
    return () => {
      closed.current = true
      socket.current?.close()
    }
  }, [connect])

  /**
   * 调一个方法，事件流通过 onEvent 回来。
   *
   * 返回的 Promise 在收到 `end` 时 resolve —— 和桌面端 `collect()` 同一个语义。
   */
  const call = useCallback(
    (method: string, params: Record<string, unknown> = {}, onEvent: (e: RemoteEvent) => void = () => {}) =>
      new Promise<void>((resolve, reject) => {
        const ws = socket.current
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          reject(new Error('还没连上电脑'))
          return
        }
        const id = nextId.current++
        waiters.current.set(id, { onEvent, resolve, reject })
        ws.send(JSON.stringify({ id, method, params }))
      }),
    []
  )

  return { state, call }
}
