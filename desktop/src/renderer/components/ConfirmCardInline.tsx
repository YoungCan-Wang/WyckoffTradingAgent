/**
 * 对话流里的就地确认卡。
 *
 * 这不是「审批」——没有队列、没有待办、没有另一个页面。写操作要动手之前，这一轮
 * 停在这里等你点一下：同意就在同一轮里执行，结果接着往下说；拒绝就到此为止。
 *
 * 卡片过期（那一轮已经按未作答收尾）时后端会回 delivered=false，此时必须说实话，
 * 不能显示「已批准」—— 那是报告一件没有发生的事。
 */
import { useState } from 'react'
import { collect } from '../lib/ipc'
import { riskReasonText, type ApprovalItem } from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  event: Record<string, unknown>
  onDecided: (toolName: string) => void
}

export function ConfirmCardInline ({ event, onDecided }: Props) {
  const [busy, setBusy] = useState(false)
  // retryable：区分「已经作过决定」和「这次点击没送到」。后者要留着按钮。
  const [outcome, setOutcome] = useState<{ kind: 'ok' | 'err'; text: string; retryable?: boolean } | null>(null)

  const questionId = String(event.question_id || '')
  const item: ApprovalItem = {
    id: questionId,
    tool_name: String(event.tool_name || event.tool || ''),
    summary: String(event.summary || ''),
    risk: String(event.risk || ''),
    risk_reason: String(event.risk_reason || ''),
    nav_ratio: typeof event.nav_ratio === 'number' ? event.nav_ratio : undefined,
    args: (event.args as Record<string, unknown>) || undefined
  }

  const decide = async (approved: boolean) => {
    setBusy(true)
    const res = await collect('chat_answer', {
      question_id: questionId,
      answer: approved ? 'allow' : 'deny'
    }).catch((err: unknown) => ({ __error: (err as Error)?.message || String(err) }))

    const errText = (res as { __error?: string } | null)?.__error
    if (errText) {
      setOutcome({ kind: 'err', text: errText, retryable: true })
      setBusy(false)
      return
    }
    // collect 失败时返回 null 而不是抛错。当成「送到了」会报告一个假的决定。
    if (!res) {
      setOutcome({ kind: 'err', text: t('confirm.callFailed'), retryable: true })
      setBusy(false)
      return
    }
    if (!(res as { delivered?: boolean }).delivered) {
      // 那一轮已经不在等了（超时收尾，或这张卡片被点过第二次）。
      setOutcome({ kind: 'err', text: t('confirm.expired') })
      setBusy(false)
      return
    }
    setOutcome({ kind: 'ok', text: approved ? t('confirm.allowed') : t('confirm.denied') })
    setBusy(false)
    // 执行结果由这一轮自己的事件流往下说；这里只负责作废受影响的缓存。
    if (approved) onDecided(String(item.tool_name || ''))
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
      <p className="sub">{t('confirm.prompt')}</p>
      {hasArgs ? (
        <details className="approval-args">
          <summary>{t('approvals.exactChange')}</summary>
          <pre>{JSON.stringify(item.args, null, 2)}</pre>
        </details>
      ) : null}
      {outcome ? (
        <div className={outcome.kind === 'err' ? 'sys err' : 'sys'}>{outcome.text}</div>
      ) : null}
      {/* 点击没送到不算决定 —— 留着按钮，否则只能重开一轮才能再试 */}
      {!outcome || outcome.retryable ? (
        <div className="btns">
          <button type="button" className="b pri" disabled={busy} onClick={() => decide(true)}>
            {busy ? t('approvals.deciding') : t('action.approve')}
          </button>
          <button type="button" className="b" disabled={busy} onClick={() => decide(false)}>
            {t('action.reject')}
          </button>
        </div>
      ) : null}
    </div>
  )
}
