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
/**
 * 一项审批的结果。
 *
 * retryable 区分「决策完成了（不管批还是拒）」和「这次调用没走通」——
 * 后者不是决策，按钮必须留着，否则这笔待批资金操作只能靠刷新页面再试。
 */
type Outcome = { kind: 'ok' | 'err'; text: string; retryable?: boolean } | null

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
      setOutcome((prev) => ({ ...prev, [item.id]: { kind: 'err', text: errText, retryable: true } }))
      setBusy((prev) => ({ ...prev, [item.id]: false }))
      return
    }
    const payload = (res || {}) as { status?: string; succeeded?: boolean }
    const status = String(payload.status || '')
    // 没有 status 意味着这次调用压根没走通（collect 失败时返回 null 而不是抛错）。
    // 原来这种情况落到 else 分支被标成「已拒绝」—— 把一个**没有发生的决策**
    // 报告成已完成，而且按钮随之消失，这笔待批操作再也点不了。
    if (!status) {
      setOutcome((prev) => ({ ...prev, [item.id]: { kind: 'err', text: t('approvals.callFailed'), retryable: true } }))
      setBusy((prev) => ({ ...prev, [item.id]: false }))
      return
    }
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

  // 只在**首次**加载时整页显示「加载中」。
  //
  // decide() 之后要 reload 才能刷掉已决策的项，而 reload 会把 loading 打回 true
  // —— 原来这一行会把整个列表（连同刚写好的「执行失败」文案）一起卸载，用户只
  // 看到列表少了一项，**看不到失败信息**。而这一页自己的设计目标就是「执行了但
  // 失败要明说失败」。
  // SchedulesPage 用页面级 notes 解决过同一个问题，但那边的项重跑后还在列表里；
  // 审批项一旦决策就离开待批列表，所以还需要下面的 decided 横幅。
  if (loading && !data) return <p className="empty">{t('tab.loading')}</p>
  if (failed) return <p className="empty">{t('approvals.callFailed')}</p>

  const items = (data && data.items) || []
  // 已经决策、但已从待批列表消失的那些 —— 它们的结果仍要留在屏幕上。
  const pendingIds = new Set(items.map((i) => i.id))
  const decided = Object.entries(outcome).filter(
    ([id, out]) => out && !out.retryable && !pendingIds.has(id)
  )
  if (!items.length && !decided.length) {
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
      {/*
        刚决策完、已经离开待批列表的那些结果。
        「已执行」也保留：用户需要确认自己那一下真的生效了，而不是列表少一项。
      */}
      {decided.map(([id, out]) => (
        <div key={`done-${id}`} className={out!.kind === 'err' ? 'sys err' : 'sys'}>
          {out!.text}
        </div>
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
        <div className="approval-evidence">
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

      {/*
        决策后按钮消失：同一项不该能批两次。
        但「调用没走通」不是决策 —— 那种情况要留着按钮，否则这笔待批资金操作
        只能靠刷新页面才能再试一次。
      */}
      {outcome ? (
        <div className={outcome.kind === 'err' ? 'sys err' : 'sys'}>{outcome.text}</div>
      ) : null}
      {!outcome || outcome.retryable ? (
        <div className="btns">
          <button type="button" className="b pri" disabled={busy} onClick={() => onDecide(item, true)}>
            {busy ? t('approvals.deciding') : t('action.approve')}
          </button>
          <button type="button" className="b" disabled={busy} onClick={() => onDecide(item, false)}>
            {t('action.reject')}
          </button>
        </div>
      ) : null}
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
