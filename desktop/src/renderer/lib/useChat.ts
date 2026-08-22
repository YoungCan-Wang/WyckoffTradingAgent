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
import { collect } from './ipc'
import { reportArtifact, type ChatArtifact } from './artifacts'
import { useArtifacts } from './useArtifacts'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

export interface ChatApi {
  turns: Turn[]
  busy: boolean
  /** 有过任何一轮 = 欢迎页该让位给对话 */
  started: boolean
  send: (text: string) => Promise<void>
  /** 清掉前后端会话历史，开始一段独立分析。 */
  reset: () => Promise<boolean>
  /** 系统提示行（退出登录、切模型失败之类）也进对话流。 */
  sysLine: (text: string, isError?: boolean) => void
  invalidateOnTool: (toolName: string) => void
  /** 本会话的全部产物 —— 对话卡片与页签共用同一份数据。 */
  artifacts: ChatArtifact[]
  /** 用户主动打开某个产物。 */
  openArtifact: (artifact: ChatArtifact) => void
}

export function useChat (ready: boolean): ChatApi {
  const artifactsApi = useArtifacts()
  const [turns, setTurns] = useState<Turn[]>([])
  const [busy, setBusy] = useState(false)
  // 事件到达时要改「当前活跃的那一轮」，用 ref 拿最新值 —— 闭包里读 state
  // 会读到订阅建立时的旧值。
  const liveIds = useRef<Set<string>>(new Set())
  /**
   * send 还在等 {ok,id} 期间到达的事件，按 id 暂存。
   *
   * 桥一收到请求就同步开始推事件，而 id 要跨进程回来 —— 所以头几个 delta
   * 一定早于我们建这一轮。它们既进不了 liveIds 判断（那时还没登记），也找不到
   * 对应的 turn，于是被静默丢掉：正文从中间某句开始，前面的段落和列表项凭空
   * 消失（界面上看不出丢了东西，这是最糟的一点）。
   */
  const pendingEvents = useRef<Map<string, Array<Record<string, unknown>>>>(new Map())
  /** 有 send 在飞行中 —— 此时遇到不认识的 id 要缓存，而不是丢掉。 */
  const sendInFlight = useRef(0)

  const invalidateOnTool = useCallback((toolName: string) => {
    if (!isPortfolioWriteTool(toolName)) return
    window.WyckoffReact?.clearPortfolioCaches?.()
  }, [])

  const handleLiveEvent = useCallback((event: Record<string, unknown>) => {
    const id = String(event.id || '')
    const type = String(event.type || '')
    if (type === 'tool_start') {
      // 改持仓的工具一开跑就作废缓存：审批可能被「本次会话总是允许」放行，
      // 那条路径不产生审批事件，只能挂在这里。
      invalidateOnTool(String(event.name || ''))
      // 刻意**不**在这里开 K 线图。
      //
      // 旧实现在 tool_start 就 openKline：工具还没成功,失败会留一个空面板;
      // 而且 action=list（只是列出标注）也会弹开图表页。现在由后端在
      // tool_result 之后发 chat_artifact 事件,useArtifacts 据它决定是否展开。
      //
      // drewCharts 仍要记：标注是在图建好之后写的,end 时要刷新一次。
      const code = (event.args as { code?: string } | undefined)?.code
      if (String(event.name || '') === 'annotate_chart' && code) {
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
        // 报告形态的回复送去产物面板，对话里留一张能重新打开的卡片。
        //
        // 原来是 `blocks.filter(b => b.kind !== 'text')` —— 把正文**整块滤掉**。
        // openReport 一旦失败（渲染抛异常、面板被关），模型生成的完整正文就彻底
        // 没了，那一轮只剩一句「已在右侧打开」，而那行是纯文本、不可点。
        // 现在把正文存进 artifact 块：面板出问题不丢东西，关掉页签也能重开。
        if (looksLikeReport(body)) {
          const title = reportTitle(body, t('chat.report'))
          // 走注册表而不是直接 openReport：自动展开策略（一轮只开第一个、
          // 用户关过不再弹、窄窗口不分栏）对报告和 K 线应当一致。
          // 直接调 openReport 会绕过那些规则。
          artifactsApi.add(reportArtifact(id, title, body))
          return {
            ...x,
            blocks: [
              ...x.blocks.filter((b) => b.kind !== 'text'),
              { kind: 'artifact', artifactKind: 'report', title, body }
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
  }, [invalidateOnTool, artifactsApi])

  useEffect(() => {
    const off = window.wyckoff.onEvent((event) => {
      const id = String(event.id || '')
      if (!id) return
      if (!liveIds.current.has(id)) {
        // 不是我们在等的流。但如果此刻有 send 在飞行，这可能正是它的前几个
        // 事件（id 还没回来）—— 缓存等回放。缓存只在飞行期间增长，send 一
        // 落地就会取走或清掉，不会无界堆积。
        if (!sendInFlight.current) return
        const list = pendingEvents.current.get(id) || []
        list.push(event)
        pendingEvents.current.set(id, list)
        return
      }
      handleLiveEvent(event)
    })
    return off
  }, [handleLiveEvent])

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
    sendInFlight.current += 1
    const res = await window.wyckoff.call('chat', { text: body }).finally(() => {
      sendInFlight.current -= 1
    })
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
    // 新一轮：重置「本轮已展开」与「本轮关过」——上一轮的关闭不该影响这一轮。
    artifactsApi.beginTurn()
    liveIds.current.add(id)
    setTurns((prev) => [...prev, { id, user: body, blocks: [], live: true }])
    // 回放 await 期间缓存下来的事件，然后把缓存清空 —— 没被认领的那些属于
    // 页面调用（它们有自己的订阅），留着只会占内存。
    const early = pendingEvents.current.get(id)
    pendingEvents.current.clear()
    if (early?.length) {
      for (const event of early) handleLiveEvent(event)
    }
  }, [busy, ready, handleLiveEvent])

  const reset = useCallback(async () => {
    if (busy || !ready) return false
    const result = await collect('chat_reset').catch(() => null)
    if (!result) {
      sysLine(t('chat.resetFailed'), true)
      return false
    }
    liveIds.current.clear()
    pendingEvents.current.clear()
    setTurns([])
    setBusy(false)
    return true
  }, [busy, ready, sysLine])

  return {
    turns,
    busy,
    started: turns.length > 0,
    send,
    reset,
    sysLine,
    invalidateOnTool,
    artifacts: artifactsApi.artifacts,
    openArtifact: artifactsApi.open
  }
}
