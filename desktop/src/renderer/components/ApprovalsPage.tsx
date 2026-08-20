/**
 * 「审批」页。
 *
 * 批准会真的执行工具，所以这页的关键不是布局而是状态诚实：
 * - 决策中禁用两个按钮，避免重复提交
 * - 执行失败要明说失败，不能因为 call 成功就报成已执行
 * - 执行成功且改了持仓 → 作废持仓缓存，否则你会对着旧持仓做下一个决定
 */
import { useCallback, useState } from 'react'
import { collect } from '../lib/ipc'
import { useIpc } from '../lib/useIpc'
import { riskReasonText, displayTime, type ApprovalItem } from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface ApprovalsData {
  count?: number
  items?: ApprovalItem[]
}

interface AccountData {
  signed_in?: boolean
  email?: string
}

/** 一项的决策结果。null = 还没决策。 */
type Outcome = { kind: 'ok' | 'err'; text: string } | null

export function ApprovalsPage () {
  const { data, loading, failed, reload } = useIpc<ApprovalsData>('approve_list')
  const { data: account } = useIpc<AccountData>('account')
  // 每一项各自的进行中/结果状态，按 id 存。
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [outcome, setOutcome] = useState<Record<string, Outcome>>({})

  const decide = useCallback(async (item: ApprovalItem, approved: boolean) => {
    setBusy((prev) => ({ ...prev, [item.id]: true }))
    setOutcome((prev) => ({ ...prev, [item.id]: null }))
    const res = await collect('approve_decide', { id: item.id, approved }).catch(
      (err: unknown) => ({ __error: (err as Error)?.message || String(err) })
    )
    const errText = (res as { __error?: string } | null)?.__error
    if (errText) {
      setOutcome((prev) => ({ ...prev, [item.id]: { kind: 'err', text: errText } }))
      setBusy((prev) => ({ ...prev, [item.id]: false }))
      return
    }
    const payload = (res || {}) as { status?: string; succeeded?: boolean }
    const status = String(payload.status || '')
    // 只看 call 成功会把「执行了但失败」报成成功 —— status 才是真相。
    const label = status === 'executed' ? t('approvals.executed')
      : status === 'failed' ? t('approvals.failed')
      : t('approvals.rejected')
    setOutcome((prev) => ({
      ...prev,
      [item.id]: { kind: status === 'failed' ? 'err' : 'ok', text: label }
    }))
    setBusy((prev) => ({ ...prev, [item.id]: false }))
    // 执行成功且动了持仓 → 缓存已脏。
    if (status === 'executed') {
      const react = window.WyckoffReact
      if (react && react.invalidatePortfolioCache) {
        react.invalidatePortfolioCache(String(item.tool_name || item.tool || ''))
      }
    }
    // 侧栏计数与列表都要跟上。
    window.WyckoffApp?.refreshApprovals?.()
    reload()
  }, [reload])

  if (loading) return <p className="empty">{t('tab.loading')}</p>
  if (failed) return <p className="empty">{t('approvals.callFailed')}</p>

  const items = (data && data.items) || []
  if (!items.length) {
    return (
      <>
        <p className="empty">{t('approvals.empty')}</p>
        <button
          type="button"
          className="task-action"
          onClick={() => window.WyckoffApp?.navigate?.('schedules')}
        >
          {t('approvals.viewSchedules')}
        </button>
      </>
    )
  }

  const accountLabel = account
    ? (account.signed_in ? (account.email || '') : t('account.signedOut'))
    : ''

  return (
    <div>
      {items.map((item) => (
        <ApprovalCard
          key={item.id}
          item={item}
          accountLabel={accountLabel}
          busy={Boolean(busy[item.id])}
          outcome={outcome[item.id] || null}
          onDecide={decide}
        />
      ))}
    </div>
  )
}

interface CardProps {
  item: ApprovalItem
  accountLabel: string
  busy: boolean
  outcome: Outcome
  onDecide: (item: ApprovalItem, approved: boolean) => void
}

function ApprovalCard ({ item, accountLabel, busy, outcome, onDecide }: CardProps) {
  const tier = ({ confirm: t('approvals.tierConfirm'), review: t('approvals.tierReview') } as
    Record<string, string>)[String(item.risk)] || item.risk || ''
  const tone = ['confirm', 'review'].includes(String(item.risk)) ? String(item.risk) : ''
  const why = riskReasonText(item)
  const hasArgs = item.args && Object.keys(item.args).length > 0
  const showEvidence = item.created_at || item.source || item.tool_name || accountLabel

  return (
    <div className="card">
      <div className="r1">
        <b>{item.summary || item.tool || item.tool_name || t('approvals.defaultItem')}</b>
        <span className={`tg ${tone}`}>{tier}</span>
      </div>
      {/* 为什么需要审批 —— 让人能判断，而不是盲批 */}
      {why ? <p className="approval-why">{why}</p> : null}
      <p className="sub">{t('approvals.submitted')}</p>

      {showEvidence ? (
        <div className="evidence">
          <Evidence label={t('approvals.tool')} value={item.tool_name || item.tool} />
          <Evidence label={t('approvals.source')} value={item.source} />
          <Evidence label={t('approvals.schedule')} value={item.schedule_id} />
          <Evidence label={t('approvals.account')} value={accountLabel} />
          <Evidence label={t('approvals.requestedAt')} value={displayTime(item.created_at)} />
        </div>
      ) : null}

      {hasArgs ? (
        <details className="approval-args">
          <summary>{t('approvals.exactChange')}</summary>
          <pre>{JSON.stringify(item.args, null, 2)}</pre>
        </details>
      ) : null}

      {/* 决策后按钮消失：同一项不该能批两次 */}
      {outcome ? (
        <div className={outcome.kind === 'err' ? 'sys err' : 'sys'}>{outcome.text}</div>
      ) : (
        <div className="btns">
          <button type="button" className="b pri" disabled={busy} onClick={() => onDecide(item, true)}>
            {busy ? t('approvals.deciding') : t('action.approve')}
          </button>
          <button type="button" className="b" disabled={busy} onClick={() => onDecide(item, false)}>
            {t('action.reject')}
          </button>
        </div>
      )}
    </div>
  )
}

function Evidence ({ label, value }: { label: string; value?: string }) {
  if (!value) return null
  return (
    <div className="evidence-item">
      <span>{label}</span>
      <b>{value}</b>
    </div>
  )
}
