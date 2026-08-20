/**
 * 对话流里的审批卡片。
 *
 * 与「审批」页的卡片同源同规则，只是嵌在对话里 —— 批准要说实话（executed /
 * failed / rejected 分开），决策后按钮消失，执行成功且动了持仓就作废缓存。
 */
import { useState } from 'react'
import { collect } from '../lib/ipc'
import { riskReasonText, type ApprovalItem } from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  event: Record<string, unknown>
  onDecided: (toolName: string) => void
}

export function ApprovalCardInline ({ event, onDecided }: Props) {
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const item: ApprovalItem = {
    id: String(event.id || ''),
    tool_name: String(event.tool_name || event.tool || ''),
    summary: String(event.summary || ''),
    risk: String(event.risk || ''),
    risk_reason: String(event.risk_reason || ''),
    nav_ratio: typeof event.nav_ratio === 'number' ? event.nav_ratio : undefined,
    args: (event.args as Record<string, unknown>) || undefined
  }

  const decide = async (approved: boolean) => {
    setBusy(true)
    const res = await collect('approve_decide', { id: item.id, approved }).catch(
      (err: unknown) => ({ __error: (err as Error)?.message || String(err) })
    )
    const errText = (res as { __error?: string } | null)?.__error
    if (errText) {
      setOutcome({ kind: 'err', text: errText })
      setBusy(false)
      return
    }
    const status = String(((res || {}) as { status?: string }).status || '')
    // 只看调用成功会把「执行了但失败」报成成功。
    const label = status === 'executed' ? t('approvals.executed')
      : status === 'failed' ? t('approvals.failed')
      : t('approvals.rejected')
    setOutcome({ kind: status === 'failed' ? 'err' : 'ok', text: label })
    setBusy(false)
    if (status === 'executed') onDecided(String(item.tool_name || ''))
    window.WyckoffApp?.refreshApprovals?.()
  }

  const tier = ({ confirm: t('approvals.tierConfirm'), review: t('approvals.tierReview') } as
    Record<string, string>)[String(item.risk)] || item.risk || ''
  const tone = ['confirm', 'review'].includes(String(item.risk)) ? String(item.risk) : ''
  const why = riskReasonText(item)
  const hasArgs = item.args && Object.keys(item.args).length > 0

  return (
    <div className="card">
      <div className="r1">
        <b>{item.summary || item.tool_name || t('approvals.defaultItem')}</b>
        <span className={`tg ${tone}`}>{tier}</span>
      </div>
      {why ? <p className="approval-why">{why}</p> : null}
      <p className="sub">{t('approvals.submitted')}</p>
      {hasArgs ? (
        <details className="approval-args">
          <summary>{t('approvals.exactChange')}</summary>
          <pre>{JSON.stringify(item.args, null, 2)}</pre>
        </details>
      ) : null}
      {outcome ? (
        <div className={outcome.kind === 'err' ? 'sys err' : 'sys'}>{outcome.text}</div>
      ) : (
        <div className="btns">
          <button type="button" className="b pri" disabled={busy} onClick={() => decide(true)}>
            {busy ? t('approvals.deciding') : t('action.approve')}
          </button>
          <button type="button" className="b" disabled={busy} onClick={() => decide(false)}>
            {t('action.reject')}
          </button>
        </div>
      )}
    </div>
  )
}
