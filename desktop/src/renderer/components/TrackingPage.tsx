/**
 * 推荐跟踪：三个市场页签 + 筛选 + 可排序表格。
 *
 * 三个市场是三张独立的表，所以切页签是重新查询而不是客户端过滤 ——
 * 美股 2363 条、港股 865 条，一次全拉进来既慢也没必要。
 *
 * 诚实规则不变：缺失的数值显示破折号，不补零。0.00% 是真的没涨跌。
 */
import { useMemo, useState } from 'react'
import { useIpc } from '../lib/useIpc'
import {
  dedupeByCode, sortRows, filterRows, displayCode,
  type TrackRecord, type Market, type SortKey, type SortDir
} from '../lib/tracking'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

const DASH = '—'
const MARKETS: Market[] = ['cn', 'us', 'hk']
const DAY_WINDOWS = [5, 10, 20, 30, 0]

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const price = (v: number | null) => (isNum(v) ? v.toFixed(2) : DASH)
const pct = (v: number | null) => (isNum(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}%` : DASH)

/** A 股惯例红涨绿跌；0 与缺失都不着色 —— 着色意味着「有方向」。 */
const moveClass = (v: number | null) => {
  if (!isNum(v) || v === 0) return 'trk-flat'
  return v > 0 ? 'trk-up' : 'trk-down'
}

function formatDay (raw: string): string {
  const d = String(raw || '').replace(/\D/g, '')
  return d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : String(raw || DASH)
}

export function TrackingPage () {
  const [market, setMarket] = useState<Market>('cn')
  const [query, setQuery] = useState('')
  const [aiOnly, setAiOnly] = useState(false)
  const [days, setDays] = useState(30)
  const [sortKey, setSortKey] = useState<SortKey>('date')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  // market 进 useIpc 的参数 → 切页签自动重查。
  const { data, loading, failed } = useIpc<{ records?: TrackRecord[]; total?: number }>(
    'tracking',
    { market, limit: 200 }
  )

  const all = useMemo(() => dedupeByCode((data && data.records) || []), [data])
  const rows = useMemo(
    () => sortRows(filterRows(all, { query, aiOnly, days }), sortKey, sortDir),
    [all, query, aiOnly, days, sortKey, sortDir]
  )

  const onSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir(sortDir === 'desc' ? 'asc' : 'desc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const rated = rows.filter((r) => isNum(r.pnl_pct))
  const avg = rated.length ? rated.reduce((s, r) => s + (r.pnl_pct as number), 0) / rated.length : null
  const winners = rated.filter((r) => (r.pnl_pct as number) > 0).length
  // 美股/港股表有 mfe/mae，A 股本地缓存没有 —— 有才显示那两列。
  const hasRange = rows.some((r) => isNum(r.max_pnl_pct) || isNum(r.min_pnl_pct))

  return (
    <>
      <div className="trk-tabs">
        {MARKETS.map((m) => (
          <button
            key={m}
            type="button"
            className={m === market ? 'trk-tab on' : 'trk-tab'}
            onClick={() => setMarket(m)}
          >
            {t(`tracking.market.${m}`)}
          </button>
        ))}
      </div>

      <div className="trk-bar">
        <input
          className="trk-search"
          placeholder={t('tracking.searchPlaceholder')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <label className="trk-check">
          <input type="checkbox" checked={aiOnly} onChange={(e) => setAiOnly(e.target.checked)} />
          {t('tracking.aiOnly')}
        </label>
        <select className="sel" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {DAY_WINDOWS.map((d) => (
            <option key={d} value={d}>
              {d === 0 ? t('tracking.allDays') : t('tracking.lastDays', { count: d })}
            </option>
          ))}
        </select>
        <span className="trk-count">
          {loading ? t('tab.loading') : t('tracking.shown', { shown: rows.length, total: all.length })}
        </span>
      </div>

      {loading ? <p className="empty">{t('tab.loading')}</p> : null}
      {/* 三种状态分开：读失败、真的没数据、筛完没结果 */}
      {!loading && failed ? <p className="empty">{t('tracking.readFailed')}</p> : null}
      {!loading && !failed && !all.length ? <p className="empty">{t('tracking.empty')}</p> : null}
      {!loading && !failed && all.length && !rows.length
        ? <p className="empty">{t('tracking.noMatch')}</p>
        : null}

      {rows.length ? (
        <>
          <div className="task-metrics">
            <Metric value={String(rows.length)} label={t('tracking.metricTotal')} />
            <Metric value={avg === null ? DASH : pct(avg)} label={t('tracking.metricAvg')} />
            <Metric
              value={rated.length ? `${Math.round((winners / rated.length) * 100)}%` : DASH}
              label={t('tracking.metricWinRate')}
            />
            <Metric value={String(rows.filter((r) => r.is_ai_recommended).length)} label={t('tracking.metricAi')} />
          </div>

          {rated.length < rows.length ? (
            <p className="trk-note">{t('tracking.partialNote', { rated: rated.length, total: rows.length })}</p>
          ) : null}

          <div className="trk-tbl">
            <table>
              <tbody>
                <tr>
                  <Th sortKey="code" active={sortKey} dir={sortDir} onSort={onSort}>{t('charts.colCode')}</Th>
                  <th>{t('charts.colName')}</th>
                  <Th sortKey="date" active={sortKey} dir={sortDir} onSort={onSort}>{t('tracking.colDate')}</Th>
                  <th className="r">{t('tracking.colInitial')}</th>
                  <th className="r">{t('tracking.colCurrent')}</th>
                  <Th sortKey="change" active={sortKey} dir={sortDir} onSort={onSort} right>
                    {t('tracking.colChange')}
                  </Th>
                  {hasRange ? (
                    <>
                      <Th sortKey="mfe" active={sortKey} dir={sortDir} onSort={onSort} right>
                        {t('tracking.colMfe')}
                      </Th>
                      <Th sortKey="mae" active={sortKey} dir={sortDir} onSort={onSort} right>
                        {t('tracking.colMae')}
                      </Th>
                    </>
                  ) : null}
                  <th>{t('tracking.colStatus')}</th>
                </tr>
                {rows.map((r) => (
                  <tr key={`${r.code}-${r.recommend_date}`}>
                    <td className="c">{displayCode(r.code, market)}</td>
                    <td>
                      {r.name || DASH}
                      {r.is_ai_recommended ? <span className="tag pri">{t('tracking.aiTag')}</span> : null}
                    </td>
                    <td className="dim">{formatDay(r.recommend_date)}</td>
                    <td className="r">{price(r.recommend_price)}</td>
                    <td className="r">{price(r.current_price)}</td>
                    <td className={`r ${moveClass(r.pnl_pct)}`}>{pct(r.pnl_pct)}</td>
                    {hasRange ? (
                      <>
                        <td className={`r ${moveClass(r.max_pnl_pct)}`}>{pct(r.max_pnl_pct)}</td>
                        <td className={`r ${moveClass(r.min_pnl_pct)}`}>{pct(r.min_pnl_pct)}</td>
                      </>
                    ) : null}
                    <td className="dim">{r.status || r.entry_role || DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
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

/** 可排序列头。当前排序列显示方向，其余不显示 —— 三个箭头一起亮会看不出主序。 */
function Th ({ sortKey, active, dir, onSort, right, children }: {
  sortKey: SortKey
  active: SortKey
  dir: SortDir
  onSort: (key: SortKey) => void
  right?: boolean
  children: React.ReactNode
}) {
  const on = sortKey === active
  return (
    <th className={right ? 'r sortable' : 'sortable'} onClick={() => onSort(sortKey)}>
      {children}
      <span className="trk-arrow">{on ? (dir === 'desc' ? '↓' : '↑') : ''}</span>
    </th>
  )
}
