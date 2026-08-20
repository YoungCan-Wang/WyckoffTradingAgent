/**
 * 对话状态机：发消息、接事件流、收尾。
 *
 * 桥不是 request/response 而是「同 id 的事件流」，所以订阅只挂一次，靠
 * event.id 分发到对应的轮次 —— 每轮各自订阅会在长会话里堆几十个监听器。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  applyEvent, finalText, looksLikeReport, reportTitle, isPortfolioWriteTool,
  type Turn
} from './chat'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

export interface ChatApi {
  turns: Turn[]
  busy: boolean
  /** 有过任何一轮 = 欢迎页该让位给对话 */
  started: boolean
  send: (text: string) => Promise<void>
  /** 系统提示行（退出登录、切模型失败之类）也进对话流。 */
  sysLine: (text: string, isError?: boolean) => void
  invalidateOnTool: (toolName: string) => void
}

export function useChat (ready: boolean): ChatApi {
  const [turns, setTurns] = useState<Turn[]>([])
  const [busy, setBusy] = useState(false)
  // 事件到达时要改「当前活跃的那一轮」，用 ref 拿最新值 —— 闭包里读 state
  // 会读到订阅建立时的旧值。
  const liveIds = useRef<Set<string>>(new Set())

  const invalidateOnTool = useCallback((toolName: string) => {
    if (!isPortfolioWriteTool(toolName)) return
    window.WyckoffReact?.clearPortfolioCaches?.()
  }, [])

  useEffect(() => {
    const off = window.wyckoff.onEvent((event) => {
      const id = String(event.id || '')
      if (!id || !liveIds.current.has(id)) return
      const type = String(event.type || '')

      if (type === 'tool_start') {
        // 改持仓的工具一开跑就作废缓存：审批可能被「本次会话总是允许」放行，
        // 那条路径不产生审批事件，只能挂在这里。
        invalidateOnTool(String(event.name || ''))
        const code = (event.args as { code?: string } | undefined)?.code
        if (String(event.name || '') === 'annotate_chart' && code) {
          window.WyckoffApp?.openKline?.(String(code))
          setTurns((prev) => prev.map((x) => (
            x.id === id
              ? { ...x, drewCharts: [...(x.drewCharts || []), String(code)] }
              : x
          )))
        }
      }

      if (type === 'approval_pending') window.WyckoffApp?.refreshApprovals?.()

      if (type === 'done') {
        setTurns((prev) => prev.map((x) => {
          if (x.id !== id) return x
          const body = finalText(x, event.text ? String(event.text) : undefined)
          // 报告形态的回复送去产物面板，对话里只留一行「已在右侧打开」。
          if (looksLikeReport(body)) {
            const title = reportTitle(body, t('chat.report'))
            window.WyckoffApp?.openReport?.(title, body)
            return {
              ...x,
              blocks: [
                ...x.blocks.filter((b) => b.kind !== 'text'),
                { kind: 'note', text: t('chat.openedRight', { title }) }
              ]
            }
          }
          // done 带了完整文本而流式一个字都没来（非流式模型）：补上。
          if (event.text && !x.blocks.some((b) => b.kind === 'text')) {
            return { ...x, blocks: [...x.blocks, { kind: 'text', text: String(event.text) }] }
          }
          return x
        }))
        return
      }

      if (type === 'end') {
        liveIds.current.delete(id)
        setBusy(false)
        setTurns((prev) => prev.map((x) => {
          if (x.id !== id) return x
          // 标注是在图建好之后写的，所以这一轮画过的图要再刷一次。
          if (x.drewCharts?.length) window.WyckoffApp?.refreshCharts?.(x.drewCharts)
          return { ...x, live: false }
        }))
        return
      }

      setTurns((prev) => prev.map((x) => (x.id === id ? applyEvent(x, event) : x)))
    })
    return off
  }, [invalidateOnTool])

  const sysLine = useCallback((text: string, isError = false) => {
    setTurns((prev) => [...prev, {
      id: `sys-${Date.now()}-${prev.length}`,
      blocks: [isError ? { kind: 'error', message: text } : { kind: 'note', text }],
      live: false
    }])
  }, [])

  const send = useCallback(async (text: string) => {
    const body = text.trim()
    if (!body || busy || !ready) return
    setBusy(true)
    const res = await window.wyckoff.call('chat', { text: body })
    if (!res.ok || !res.id) {
      // 用户那句话仍要留在流里，否则他会以为自己没发出去。
      setTurns((prev) => [...prev, {
        id: `fail-${Date.now()}`,
        user: body,
        blocks: [{ kind: 'error', message: String(res.error || t('chat.sendFailed')) }],
        live: false
      }])
      setBusy(false)
      return
    }
    // 必须 String()：桥回的 id 是**数字**（python-bridge 用自增计数器），而
    // 事件分发那边比的是 String(event.id)。混着用会让 Set.has('9') 对 9 恒为
    // 假 —— 每条事件都被丢掉，界面永远停在「正在思考…」。
    const id = String(res.id)
    liveIds.current.add(id)
    setTurns((prev) => [...prev, { id, user: body, blocks: [], live: true }])
  }, [busy, ready])

  return { turns, busy, started: turns.length > 0, send, sysLine, invalidateOnTool }
}
