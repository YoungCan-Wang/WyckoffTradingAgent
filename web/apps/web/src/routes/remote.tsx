/**
 * 手机遥控页（PWA）。
 *
 * 扫码进来之后这里就是完整的遥控器：对话、看持仓、审批。权限和电脑端一致 ——
 * 手机上能做的事和坐在电脑前一样。
 *
 * 布局是**移动优先**的，不复用桌面端组件：那些是宽屏 + 图表为中心的布局，塞进
 * 375px 只会处处将就。这里只有三样东西（消息流、输入框、底部三个页签），每一样
 * 都按拇指操作设计。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useRemote, type RemoteEvent } from '@/features/remote/use-remote'

interface Turn {
  id: number
  question: string
  answer: string
  running: boolean
  error?: string
}

interface Position {
  code: string
  name: string
  shares: number
  cost_price: number
  stop_loss: number | null
}

interface Approval {
  id: string
  summary: string
  tool_name: string
  risk: string
}

type Tab = 'chat' | 'portfolio' | 'approvals'

export function RemotePage () {
  const { state, call } = useRemote()
  const [tab, setTab] = useState<Tab>('chat')

  return (
    <div className="flex h-[100dvh] flex-col bg-stone-50">
      <Header state={state} />
      <main className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'chat' ? <ChatTab call={call} ready={state === 'paired'} /> : null}
        {tab === 'portfolio' ? <PortfolioTab call={call} ready={state === 'paired'} /> : null}
        {tab === 'approvals' ? <ApprovalsTab call={call} ready={state === 'paired'} /> : null}
      </main>
      <TabBar tab={tab} onChange={setTab} />
    </div>
  )
}

function Header ({ state }: { state: string }) {
  const label: Record<string, string> = {
    connecting: '正在连接电脑…',
    paired: '已连上电脑',
    host_offline: '电脑不在线',
    unauthorized: '需要重新扫码',
    failed: '连不上',
  }
  const ok = state === 'paired'
  return (
    <header className="flex items-center gap-2 border-b border-stone-200 bg-white px-4 py-3">
      <span className={`h-2 w-2 shrink-0 rounded-full ${ok ? 'bg-emerald-500' : 'bg-stone-400'}`} aria-hidden />
      <span className="text-sm text-stone-700">{label[state] || state}</span>
      {state === 'host_offline' ? (
        // 说清楚为什么，否则用户会以为是手机的问题
        <span className="ml-auto text-xs text-stone-500">电脑睡眠或已退出</span>
      ) : null}
    </header>
  )
}

function ChatTab ({ call, ready }: { call: ReturnType<typeof useRemote>['call']; ready: boolean }) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const bottom = useRef<HTMLDivElement | null>(null)

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns])

  const send = useCallback(async () => {
    const question = text.trim()
    if (!question || busy || !ready) return
    const id = Date.now()
    setText('')
    setBusy(true)
    setTurns((prev) => [...prev, { id, question, answer: '', running: true }])

    const patch = (fn: (t: Turn) => Turn) =>
      setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)))

    try {
      await call('chat', { text: question }, (event: RemoteEvent) => {
        if (event.type === 'text_delta') {
          patch((t) => ({ ...t, answer: t.answer + String(event.text || '') }))
        } else if (event.type === 'done') {
          // done 带全文。流式已经拼好了，只在缺失时兜底 —— 否则会重复一遍。
          patch((t) => ({ ...t, answer: t.answer || String(event.text || ''), running: false }))
        } else if (event.type === 'error') {
          patch((t) => ({ ...t, running: false, error: String(event.message || '出错了') }))
        }
      })
    } catch (err) {
      patch((t) => ({ ...t, running: false, error: err instanceof Error ? err.message : '发送失败' }))
    } finally {
      patch((t) => ({ ...t, running: false }))
      setBusy(false)
    }
  }, [text, busy, ready, call])

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-4 px-4 py-4">
        {turns.length === 0 ? (
          <p className="pt-8 text-center text-sm text-stone-500">
            问点什么 —— 这台手机连着你电脑上的助手。
          </p>
        ) : null}
        {turns.map((turn) => (
          <div key={turn.id} className="space-y-2">
            <p className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-stone-800 px-3.5 py-2 text-[15px] text-white">
              {turn.question}
            </p>
            {turn.error ? (
              <p className="text-[15px] text-red-600">{turn.error}</p>
            ) : (
              <p className="whitespace-pre-wrap text-[15px] leading-relaxed text-stone-800">
                {turn.answer}
                {turn.running && !turn.answer ? <span className="text-stone-400">正在想…</span> : null}
              </p>
            )}
          </div>
        ))}
        <div ref={bottom} />
      </div>

      {/* 输入区贴底，留出 iOS 的安全区 —— 否则输入框会被 home 指示条压住 */}
      <div className="sticky bottom-0 border-t border-stone-200 bg-white px-3 pb-[env(safe-area-inset-bottom)] pt-3">
        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={ready ? '随便问……' : '等待连接…'}
            disabled={!ready}
            rows={1}
            // 16px 是 iOS 的下限：更小的字号会让 Safari 在聚焦时自动放大整个页面
            className="max-h-32 min-h-[44px] flex-1 resize-none rounded-2xl border border-stone-300 px-3.5 py-2.5 text-base outline-none focus:border-stone-500"
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={!ready || busy || !text.trim()}
            // 44px 是可靠的触摸目标下限
            className="h-11 w-11 shrink-0 rounded-full bg-stone-800 text-white disabled:bg-stone-300"
            aria-label="发送"
          >
            ↑
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * 价格显示。
 *
 * 止损价是算出来的（按比例推的），实测拿到 `3.2557142857142853` 这种 17 位小数 ——
 * 在 390px 宽的手机上它会把整行挤爆，而多出的那些位一个都没有意义。
 * 两位小数是股价的实际精度。
 */
function money (value: number): string {
  return Number(value).toFixed(2)
}

function PortfolioTab ({ call, ready }: { call: ReturnType<typeof useRemote>['call']; ready: boolean }) {
  const [rows, setRows] = useState<Position[] | null>(null)
  const [cash, setCash] = useState(0)
  const [failed, setFailed] = useState(false)

  const load = useCallback(async () => {
    if (!ready) return
    setFailed(false)
    try {
      await call('portfolio', {}, (event) => {
        if (event.type !== 'result') return
        const p = (event.portfolio || {}) as { positions?: Position[]; free_cash?: number }
        setRows(p.positions || [])
        setCash(Number(p.free_cash || 0))
      })
    } catch {
      setFailed(true)
    }
  }, [call, ready])

  useEffect(() => { void load() }, [load])

  if (failed) return <Empty text="读取持仓失败" action="重试" onAction={() => void load()} />
  if (rows === null) return <Empty text="读取中…" />
  if (rows.length === 0) return <Empty text="还没有持仓" />

  return (
    <div className="px-4 py-4">
      <p className="pb-3 text-sm text-stone-600">可用现金 {cash.toLocaleString()}</p>
      <div className="space-y-2">
        {rows.map((p) => (
          <div key={p.code} className="rounded-xl border border-stone-200 bg-white p-3.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[15px] font-medium text-stone-900">{p.name || p.code}</span>
              <span className="font-mono text-xs text-stone-500">{p.code}</span>
            </div>
            <div className="pt-1.5 text-sm text-stone-600">
              {p.shares} 股 · 成本 {money(p.cost_price)}
              {p.stop_loss === null ? (
                // 缺止损是要行动的信号，不是普通字段缺失
                <span className="pl-2 text-amber-700">未设止损</span>
              ) : (
                <span className="pl-2">止损 {money(p.stop_loss)}</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ApprovalsTab ({ call, ready }: { call: ReturnType<typeof useRemote>['call']; ready: boolean }) {
  const [items, setItems] = useState<Approval[] | null>(null)
  const [deciding, setDeciding] = useState('')
  const [note, setNote] = useState('')

  const load = useCallback(async () => {
    if (!ready) return
    try {
      await call('approve_list', {}, (event) => {
        if (event.type === 'result') setItems((event.items as Approval[]) || [])
      })
    } catch {
      setItems([])
    }
  }, [call, ready])

  useEffect(() => { void load() }, [load])

  const decide = async (id: string, approved: boolean) => {
    // 批准即执行，动的是真钱。手机上误触的概率比鼠标高，所以一律二次确认。
    if (!window.confirm(approved ? '批准并立即执行？' : '拒绝这项操作？')) return
    setDeciding(id)
    setNote('')
    try {
      await call('approve_decide', { id, approved }, (event) => {
        if (event.type === 'result') {
          setNote(event.succeeded === false ? '执行失败' : approved ? '已执行' : '已拒绝')
        } else if (event.type === 'error') {
          // 电脑上刚批过同一项时会走到这里 —— 说清楚而不是报「失败」
          setNote(String(event.code) === 'not_actionable' ? '这项已经处理过了' : String(event.message || '操作失败'))
        }
      })
    } finally {
      setDeciding('')
      void load()
    }
  }

  if (items === null) return <Empty text="读取中…" />
  if (items.length === 0) return <Empty text="没有待批准的操作" />

  return (
    <div className="space-y-3 px-4 py-4">
      {note ? <p className="text-sm text-stone-600">{note}</p> : null}
      {items.map((item) => (
        <div key={item.id} className="rounded-xl border border-stone-200 bg-white p-3.5">
          <p className="text-[15px] text-stone-900">{item.summary || item.tool_name}</p>
          <p className="pt-1 text-xs text-stone-500">{item.tool_name}</p>
          <div className="flex gap-2 pt-3">
            <button
              type="button"
              disabled={deciding === item.id}
              onClick={() => void decide(item.id, true)}
              className="h-11 flex-1 rounded-xl bg-stone-800 text-[15px] text-white disabled:bg-stone-300"
            >
              批准并执行
            </button>
            <button
              type="button"
              disabled={deciding === item.id}
              onClick={() => void decide(item.id, false)}
              className="h-11 flex-1 rounded-xl border border-stone-300 text-[15px] text-stone-700"
            >
              拒绝
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

function Empty ({ text, action, onAction }: { text: string; action?: string; onAction?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 px-4 pt-16">
      <p className="text-sm text-stone-500">{text}</p>
      {action ? (
        <button type="button" onClick={onAction} className="h-11 rounded-xl border border-stone-300 px-5 text-sm">
          {action}
        </button>
      ) : null}
    </div>
  )
}

function TabBar ({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const tabs: Array<[Tab, string]> = [['chat', '对话'], ['portfolio', '持仓'], ['approvals', '审批']]
  return (
    <nav className="flex border-t border-stone-200 bg-white pb-[env(safe-area-inset-bottom)]">
      {tabs.map(([key, label]) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          aria-current={tab === key ? 'page' : undefined}
          className={`h-14 flex-1 text-sm ${tab === key ? 'font-semibold text-stone-900' : 'text-stone-500'}`}
        >
          {label}
        </button>
      ))}
    </nav>
  )
}
