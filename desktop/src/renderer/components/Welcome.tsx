/**
 * 欢迎页：问候 + 今日概览 + 需要处理 + 快速开始。
 *
 * 概览只汇总真实状态 —— 某个调用失败时那一项显示为空，绝不编数字。
 */
import { useEffect, useState } from 'react'
import { collect } from '../lib/ipc'
import { Composer } from './Composer'
import type { Portfolio } from '../types'

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
  noStop: number
  pending: number
  enabled: number
  daemonRunning: boolean
  summary: string
}

export function Welcome ({ draft, onDraft, onSend, busy, ready, sendOnEnter }: Props) {
  const [ov, setOv] = useState<Overview | null>(null)

  useEffect(() => {
    let alive = true
    void (async () => {
      const [approvals, pf, schedules] = await Promise.all([
        collect('approve_list').catch(() => null),
        collect('portfolio').catch(() => null),
        collect('schedules').catch(() => null)
      ])
      if (!alive) return
      const pending = Number((approvals as { count?: number } | null)?.count || 0)
      const portfolio = (pf as { portfolio?: Portfolio } | null)?.portfolio
      const positions = portfolio?.positions || []
      // 没设止损的仓位 —— 这是最值得先看一眼的风险
      const noStop = positions.filter((p) => p.stop_loss === null || p.stop_loss === undefined).length
      const sch = (schedules as { schedules?: Array<{ enabled?: boolean }>; daemon_running?: boolean } | null)
      const enabled = (sch?.schedules || []).filter((s) => s.enabled).length

      const parts: string[] = []
      parts.push(positions.length ? t('welcome.holding', { count: positions.length }) : t('welcome.noHolding'))
      if (noStop) parts.push(t('welcome.noStop', { count: noStop }))
      parts.push(pending ? t('welcome.pending', { count: pending }) : t('welcome.noPending'))

      setOv({
        positions: positions.length,
        noStop,
        pending,
        enabled,
        daemonRunning: Boolean(sch?.daemon_running),
        summary: parts.join(' · ')
      })
    })()
    return () => { alive = false }
  }, [])

  const fill = (key: string) => {
    // 预填而不是直接发：这些动作碰钱，让用户自己按发送。
    onDraft(t(key))
    document.getElementById('input')?.focus()
  }

  const attention: React.ReactNode[] = []
  if (ov?.pending) {
    attention.push(
      <button key="ap" type="button" onClick={() => window.WyckoffApp?.navigate?.('approvals')}>
        {t('welcome.approvalAttention', { count: ov.pending })}
      </button>
    )
  }
  if (ov?.noStop) {
    attention.push(
      <button key="st" type="button" onClick={() => fill('prompt.stops')}>
        {t('welcome.stopAttention', { count: ov.noStop })}
      </button>
    )
  }
  if (ov?.enabled && !ov.daemonRunning) {
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
        <p className="wel-s" id="wel-sum">{ov ? ov.summary : ''}</p>

        <Composer
          value={draft}
          onChange={onDraft}
          onSend={onSend}
          busy={busy}
          ready={ready}
          sendOnEnter={sendOnEnter}
        />

        <div className="wel-overview" id="wel-overview">
          {ov ? (
            <>
              <Metric label={t('welcome.positionsMetric')} value={ov.positions} view="portfolio" />
              <Metric label={t('welcome.approvalsMetric')} value={ov.pending} view="approvals" />
              <Metric label={t('welcome.schedulesMetric')} value={ov.enabled} view="tasks" />
              <Metric label={t('welcome.riskMetric')} value={ov.noStop} view="portfolio" />
            </>
          ) : null}
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

function Metric ({ label, value, view }: { label: string; value: number; view: string }) {
  return (
    <button type="button" className="wel-metric" onClick={() => window.WyckoffApp?.navigate?.(view)}>
      <b className="tnum">{String(value)}</b>
      <span>{label}</span>
    </button>
  )
}
