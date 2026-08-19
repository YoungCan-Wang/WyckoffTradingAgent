/**
 * 推荐跟踪页：过去推荐过的票，以及它们后来怎么走。
 *
 * 诚实规则：缺失的数值一律显示破折号，绝不填 0 —— 「涨跌 0%」和「还没评估」
 * 是两件完全不同的事，后者填 0 就是编数字。
 */
import { useIpc } from '../lib/useIpc'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface TrackRecord {
  code: string
  name: string
  recommend_date: string
  recommend_price: number | null
  current_price: number | null
  pnl_pct: number | null
  max_pnl_pct: number | null
  min_pnl_pct: number | null
  camp: string
  status: string
  is_ai_recommended: boolean
  entry_role: string
}

const DASH = '—'

const isNum = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)

const price = (value: number | null) => (isNum(value) ? value.toFixed(2) : DASH)
const pct = (value: number | null) => (isNum(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : DASH)

/** A 股惯例：红涨绿跌，与 charts.js 一致。0 和缺失都不着色。 */
const moveClass = (value: number | null) => {
  if (!isNum(value) || value === 0) return 'trk-flat'
  return value > 0 ? 'trk-up' : 'trk-down'
}

/** 推荐日在库里是 20260818 这种紧凑写法，显示成有分隔的形式更好扫。 */
function formatDay (raw: string): string {
  const digits = String(raw || '').replace(/\D/g, '')
  if (digits.length !== 8) return String(raw || DASH)
  return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6)}`
}

export function TrackingPage () {
  const { data, loading, failed } = useIpc<{ records?: TrackRecord[]; total?: number; message?: string }>(
    'tracking',
    { limit: 50 }
  )

  if (loading) return <p className="empty">{t('tab.loading')}</p>
  // 失败与空态必须分开：混在一起会把「读不到」说成「你没有记录」。
  if (failed) return <p className="empty">{t('tracking.readFailed')}</p>

  const records = (data && data.records) || []
  if (!records.length) return <p className="empty">{t('tracking.empty')}</p>

  const rated = records.filter((r) => isNum(r.pnl_pct))
  const avg = rated.length
    ? rated.reduce((sum, r) => sum + (r.pnl_pct as number), 0) / rated.length
    : null
  const winners = rated.filter((r) => (r.pnl_pct as number) > 0).length
  const aiCount = records.filter((r) => r.is_ai_recommended).length

  // 按推荐日倒序分组：同一天推的票放一起，最近的在上面。
  const byDay = new Map<string, TrackRecord[]>()
  for (const record of records) {
    const day = String(record.recommend_date || '')
    if (!byDay.has(day)) byDay.set(day, [])
    ;(byDay.get(day) as TrackRecord[]).push(record)
  }
  const days = [...byDay.keys()].sort((a, b) => b.localeCompare(a))

  return (
    <>
      <div className="task-metrics">
        <Metric value={String(records.length)} label={t('tracking.metricTotal')} />
        {/* 没有任何已评估的行时给破折号，而不是 0.00% */}
        <Metric value={avg === null ? DASH : pct(avg)} label={t('tracking.metricAvg')} />
        <Metric
          value={rated.length ? `${Math.round((winners / rated.length) * 100)}%` : DASH}
          label={t('tracking.metricWinRate')}
        />
        <Metric value={String(aiCount)} label={t('tracking.metricAi')} />
      </div>

      {rated.length < records.length ? (
        <p className="trk-note">
          {t('tracking.partialNote', { rated: rated.length, total: records.length })}
        </p>
      ) : null}

      {days.map((day) => {
        const rows = byDay.get(day) as TrackRecord[]
        return (
          <section className="task-section" key={day}>
            <div className="task-section-h">
              {formatDay(day)}
              <span className="trk-count">{t('tracking.dayCount', { count: rows.length })}</span>
            </div>
            <div className="task-list">
              {rows.map((record) => (
                <Row key={`${day}-${record.code}`} record={record} />
              ))}
            </div>
          </section>
        )
      })}
    </>
  )
}

function Metric ({ value, label }: { value: string; label: string }) {
  return (
    <div className="task-metric">
      <b className="tnum">{value}</b>
      <span>{label}</span>
    </div>
  )
}

function Row ({ record }: { record: TrackRecord }) {
  const hasRange = isNum(record.max_pnl_pct) || isNum(record.min_pnl_pct)
  return (
    <div className="task-row trk-row">
      <div className="task-title">
        <span className="trk-code">{record.code}</span>
        <span className="trk-name">{record.name || DASH}</span>
        {record.is_ai_recommended ? <span className="tag pri">{t('tracking.aiTag')}</span> : null}
      </div>
      <div className="task-meta">
        {price(record.recommend_price)} → {price(record.current_price)}
        {record.camp ? ` · ${record.camp}` : ''}
        {/* 区间只在有数据时出现：本地缓存没有 mfe/mae 列，硬显示会全是破折号 */}
        {hasRange ? ` · ${t('tracking.range')} ${pct(record.max_pnl_pct)} / ${pct(record.min_pnl_pct)}` : ''}
      </div>
      <div className="task-side">
        <span className={`trk-move ${moveClass(record.pnl_pct)}`}>{pct(record.pnl_pct)}</span>
        <span className="task-time">{record.status || record.entry_role || DASH}</span>
      </div>
    </div>
  )
}
