/**
 * 欢迎页：问候 + 今日概览 + 需要处理 + 快速开始。
 *
 * 概览只汇总真实状态 —— 某个调用失败时那一项显示为空，绝不编数字。
 */
import { useEffect, useState } from 'react'
import { collect } from '../lib/ipc'
import { usePortfolio } from '../lib/usePortfolio'
import { Composer } from './Composer'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

// [标签 key, 提示词 key]；渲染时才解析，语言切换后跟着变。
const PROMPTS: Array<[string, string]> = [
  ['prompt.todayName', 'prompt.today'],
  ['prompt.holdingsName', 'prompt.holdings'],
  ['prompt.stopsName', 'prompt.stops'],
  ['prompt.reviewName', 'prompt.review']
]

function greeting (): string {
  const h = new Date().getHours()
  if (h < 6) return t('welcome.night')
  if (h < 12) return t('welcome.morning')
  if (h < 18) return t('welcome.afternoon')
  return t('welcome.evening')
}

interface Props {
  draft: string
  onDraft: (v: string) => void
  onSend: () => void
  busy: boolean
  ready: boolean
  sendOnEnter: boolean
}

interface Overview {
  positions: number
  /** 持仓还没加载出来。用于区分「0 持仓」和「还不知道」。 */
  pfUnknown: boolean
  noStop: number
  enabled: number
  daemonRunning: boolean
  summary: string
}

/**
 * 计划任务这类「非持仓」的概览数据。持仓走共享 store,不在这里。
 *
 * 原来还有一个「待审 N」。确认已经回到对话里当场问,桌面端没有待办列表了,
 * 而一个点进去无事可做的数字比没有更糟。
 */
interface SideCounts {
  enabled: number
  daemonRunning: boolean
}

export function Welcome ({ draft, onDraft, onSend, busy, ready, sendOnEnter }: Props) {
  const [side, setSide] = useState<SideCounts | null>(null)
  // 持仓订阅共享 store —— 与持仓页看的是同一份数据,它变这里就跟着变。
  // 原来这里自己 collect('portfolio'),deps 是 [],而 ChatView 用 hidden
  // 常驻挂载,于是那次请求**开机时跑一次就永不再跑**:登录之后、在持仓页
  // 拉到数据之后,首页都还是开机那一刻的空值,所以永远显示 0。
  const { portfolio, loading: pfLoading } = usePortfolio()

  useEffect(() => {
    let alive = true
    void (async () => {
      const schedules = await collect('schedules').catch(() => null)
      if (!alive) return
      const sch = (schedules as { schedules?: Array<{ enabled?: boolean }>; daemon_running?: boolean } | null)
      setSide({
        enabled: (sch?.schedules || []).filter((s) => s.enabled).length,
        daemonRunning: Boolean(sch?.daemon_running)
      })
    })()
    return () => { alive = false }
  }, [])

  // 从持仓派生,不再存进 state —— 派生值存 state 就会有「谁先到」的时序问题。
  const positions = portfolio?.positions || []
  // 没设止损的仓位 —— 这是最值得先看一眼的风险
  const noStop = positions.filter((p) => p.stop_loss === null || p.stop_loss === undefined).length

  // 还没拿到持仓时不要说「当前无持仓」—— 那是断言,而此刻只是不知道。
  // 后端冷启动要十几秒,期间说错话比什么都不说更糟。
  const pfUnknown = pfLoading && !portfolio
  const summaryParts: string[] = []
  if (!pfUnknown) {
    summaryParts.push(positions.length ? t('welcome.holding', { count: positions.length }) : t('welcome.noHolding'))
  }
  if (noStop) summaryParts.push(t('welcome.noStop', { count: noStop }))

  // 不等 side:持仓先到就先显示。整块 gate 在 side 上的话,计划慢一步
  // 就会连持仓数字一起压住 —— 那又变成「明明有数据却显示不出来」。
  const ov: Overview = {
    positions: positions.length,
    pfUnknown,
    noStop,
    enabled: side?.enabled || 0,
    daemonRunning: Boolean(side?.daemonRunning),
    summary: summaryParts.join(' · ')
  }

  const fill = (key: string) => {
    // 预填而不是直接发：这些动作碰钱，让用户自己按发送。
    onDraft(t(key))
    document.getElementById('input')?.focus()
  }

  const attention: React.ReactNode[] = []
  if (ov.noStop) {
    attention.push(
      <button key="st" type="button" onClick={() => fill('prompt.stops')}>
        {t('welcome.stopAttention', { count: ov.noStop })}
      </button>
    )
  }
  if (ov.enabled && !ov.daemonRunning) {
    attention.push(
      <button key="dm" type="button" onClick={() => window.WyckoffApp?.navigate?.('schedules')}>
        {t('welcome.schedulerAttention')}
      </button>
    )
  }

  return (
    <div className="wel" id="welcome">
      <div className="wel-in">
        <h1 className="wel-t" id="wel-greet">{greeting()}</h1>
        <p className="wel-s" id="wel-sum">{ov.summary}</p>

        <Composer
          value={draft}
          onChange={onDraft}
          onSend={onSend}
          busy={busy}
          ready={ready}
          sendOnEnter={sendOnEnter}
        />

        <div className="wel-overview" id="wel-overview">
          <Metric label={t('welcome.positionsMetric')} value={ov.pfUnknown ? null : ov.positions} view="portfolio" />
          <Metric label={t('welcome.schedulesMetric')} value={ov.enabled} view="tasks" />
          <Metric label={t('welcome.riskMetric')} value={ov.pfUnknown ? null : ov.noStop} view="portfolio" />
        </div>

        <div className="wel-attention" id="wel-attention" hidden={!attention.length}>
          <div className="wel-attention-title">{t('welcome.needsAttention')}</div>
          {attention}
        </div>

        <div className="wel-label">{t('welcome.quickStart')}</div>
        <div className="wel-g" id="wel-cards">
          {PROMPTS.map(([labelKey, promptKey]) => (
            <button
              key={labelKey}
              type="button"
              className="wel-c"
              title={t(promptKey)}
              onClick={() => fill(promptKey)}
            >
              {t(labelKey)}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function Metric ({ label, value, view }: { label: string; value: number | null; view: string }) {
  return (
    <button type="button" className="wel-metric" onClick={() => window.WyckoffApp?.navigate?.(view)}>
      {/* null = 还不知道。显示占位符而不是 0 —— 0 是一个断言。 */}
      <b className="tnum">{value === null ? '–' : String(value)}</b>
      <span>{label}</span>
    </button>
  )
}
