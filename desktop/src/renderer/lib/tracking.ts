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
 * 同一代码只保留一条：取最新推荐日那条。
 *
 * web 端同样去重（dedupeTrackingRows）。不去重的话一只票被连续推荐五天就占
 * 五行，把别的票挤出屏幕，而这五行说的是同一件事。
 *
 * initial_price 沿用最早那条 —— 那才是「推荐时的价格」，用最新一条的初始价
 * 算涨跌等于把已经走过的一段抹掉。
 */
export function dedupeByCode (rows: TrackRecord[]): TrackRecord[] {
  const byCode = new Map<string, TrackRecord>()
  for (const row of rows) {
    const key = String(row.code || '')
    if (!key) continue
    const prev = byCode.get(key)
    if (!prev) {
      byCode.set(key, row)
      continue
    }
    const newer = String(row.recommend_date || '') > String(prev.recommend_date || '')
    if (!newer) continue
    // 换成更新那条，但保留最早的推荐价。
    byCode.set(key, { ...row, recommend_price: prev.recommend_price ?? row.recommend_price })
  }
  return [...byCode.values()]
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
