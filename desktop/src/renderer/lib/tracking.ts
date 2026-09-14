/**
 * 跟踪表的数据处理：去重、排序、筛选。
 *
 * 规则照 web 端（web/apps/web/src/routes/tracking.tsx）搬过来，两端结论必须
 * 一致 —— 同一份数据在两个客户端排出不同顺序会让人怀疑哪个是对的。
 */

export interface TrackRecord {
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

export type Market = 'cn' | 'us' | 'hk'
export type SortKey = 'date' | 'change' | 'mfe' | 'mae' | 'code'
export type SortDir = 'asc' | 'desc'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/**
 * 跨日入选保留多行。只丢掉空代码，并按该行自己的事件价重算涨跌。
 */
export function dedupeByCode (rows: TrackRecord[]): TrackRecord[] {
  return rows.filter((row) => String(row.code || '')).map(withEventReturn)
}

function withEventReturn (record: TrackRecord): TrackRecord {
  const base = record.recommend_price
  const current = record.current_price
  const canCompute = isNum(base) && base !== 0 && isNum(current)
  return {
    ...record,
    pnl_pct: canCompute ? ((current - base) / base) * 100 : record.pnl_pct
  }
}

/**
 * 空值永远沉底，与升降序无关。
 *
 * 方向只作用在「两边都是数字」的分支 —— 否则升序时一屏全是没数据的行，
 * 用户以为筛坏了。这与 web 端的 nullableNumberCompare 同义。
 */
function compareNullable (a: number | null, b: number | null, dir: number): number {
  if (isNum(a) && isNum(b)) return (b - a) * dir
  if (isNum(a)) return -1
  if (isNum(b)) return 1
  return 0
}

export function sortRows (rows: TrackRecord[], key: SortKey, dir: SortDir): TrackRecord[] {
  const d = dir === 'desc' ? 1 : -1
  // 复制一份再排：原数组可能是 state，就地排序不会触发重渲染。
  return [...rows].sort((a, b) => {
    switch (key) {
      case 'change': return compareNullable(a.pnl_pct, b.pnl_pct, d)
      case 'mfe': return compareNullable(a.max_pnl_pct, b.max_pnl_pct, d)
      case 'mae': return compareNullable(a.min_pnl_pct, b.min_pnl_pct, d)
      case 'code': return String(a.code).localeCompare(String(b.code)) * -d
      default: {
        const cmp = String(b.recommend_date || '').localeCompare(String(a.recommend_date || '')) * d
        // 同日按代码兜底，避免顺序在每次渲染间跳动。
        return cmp !== 0 ? cmp : String(a.code).localeCompare(String(b.code))
      }
    }
  })
}

export interface Filters {
  query: string
  aiOnly: boolean
  /** 只看最近 N 个推荐日；0 表示不限。 */
  days: number
}

export function filterRows (rows: TrackRecord[], filters: Filters): TrackRecord[] {
  let out = rows

  if (filters.days > 0) {
    // 按「最近 N 个推荐日」而不是日历天数：休市日不该占额度。
    const dates = [...new Set(out.map((r) => String(r.recommend_date || '')))]
      .sort((a, b) => b.localeCompare(a))
      .slice(0, filters.days)
    const keep = new Set(dates)
    out = out.filter((r) => keep.has(String(r.recommend_date || '')))
  }

  if (filters.aiOnly) out = out.filter((r) => r.is_ai_recommended)

  const q = filters.query.trim().toLowerCase()
  if (q) {
    // 代码也转小写再比 —— web 端漏了这一步，导致美股大写代码搜不到。
    out = out.filter((r) =>
      String(r.code || '').toLowerCase().includes(q) ||
      String(r.name || '').toLowerCase().includes(q)
    )
  }

  return out
}

/** A 股代码在库里可能是 998 这种，补零到 6 位才是常见写法。 */
export function displayCode (code: string, market: Market): string {
  const raw = String(code || '')
  if (market !== 'cn') return raw
  return /^\d{1,6}$/.test(raw) ? raw.padStart(6, '0') : raw
}
