/**
 * 「确认记录」页。
 *
 * 只读流水：写操作在对话里当场确认，这里记的是「确认过什么、当时怎么决定的」。
 * 刻意没有按钮 —— 决定发生在对话那一轮，事后再补一个操作入口就又变成审批流程了。
 */
import { useIpc } from '../lib/useIpc'
import { riskReasonText, displayTime, type ApprovalItem } from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface RecordItem extends ApprovalItem {
  status?: string
  decided_at?: string
}

interface RecordsData {
  count?: number
  items?: RecordItem[]
}

const STATUS_KEY: Record<string, string> = {
  approved: 'records.statusApproved',
  rejected: 'records.statusRejected',
  expired: 'records.statusExpired'
}

export function RecordsPage () {
  const { data, loading, failed } = useIpc<RecordsData>('approve_records')

  if (loading && !data) return <p className="empty">{t('tab.loading')}</p>
  if (failed) return <p className="empty">{t('confirm.callFailed')}</p>

  const items = (data && data.items) || []
  if (!items.length) return <p className="empty">{t('records.empty')}</p>

  return (
    <div>
      {items.map((item) => <RecordCard key={item.id} item={item} />)}
    </div>
  )
}

function RecordCard ({ item }: { item: RecordItem }) {
  const status = String(item.status || '')
  const label = t(STATUS_KEY[status] || 'records.statusExpired')
  // 拒绝和未作答都意味着「没执行」,和已同意视觉上要能一眼分开。
  const tone = status === 'approved' ? '' : 'review'
  const why = riskReasonText(item)
  const hasArgs = item.args && Object.keys(item.args).length > 0

  return (
    <div className="card">
      <div className="r1">
        <b>{item.summary || item.tool_name || item.tool || t('approvals.defaultItem')}</b>
        <span className={`tg ${tone}`}>{label}</span>
      </div>
      {why ? <p className="approval-why">{why}</p> : null}
      <div className="approval-evidence">
        <Evidence label={t('approvals.tool')} value={item.tool_name || item.tool} />
        <Evidence label={t('approvals.source')} value={item.source} />
        <Evidence label={t('approvals.requestedAt')} value={displayTime(item.decided_at || item.created_at)} />
      </div>
      {hasArgs ? (
        <details className="approval-args">
          <summary>{t('approvals.exactChange')}</summary>
          <pre>{JSON.stringify(item.args, null, 2)}</pre>
        </details>
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
