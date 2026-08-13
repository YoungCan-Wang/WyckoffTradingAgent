import { normalizeCode } from './agent-market'

export const PATTERN_REVIEW_EMPTY_MESSAGE = '暂无形态复盘记录'
export const PATTERN_REVIEW_SCOPE_NOTE = 'AI推荐才进入交易研判，观察/信号复盘不等于买入。'

export interface PatternReviewRow {
  code: string | number
  name: string
  recommend_date: string | number
  recommend_count?: number | null
  initial_price?: number | null
  current_price?: number | null
  change_pct?: number | null
  is_ai_recommended?: boolean | number | string | null
  candidate_lane?: string | null
  entry_type?: string | null
  signal_key?: string | null
  candidate_status?: string | null
  mainline_score?: number | null
  source_type?: string | null
  signal_status?: string | null
  signal_type?: string | null
}

function isAiRecommended(value: PatternReviewRow['is_ai_recommended']): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') {
    return ['1', 'true', 't', 'yes', 'y', 'ai', 'ai推荐'].includes(value.trim().toLowerCase())
  }
  return false
}

function formatPrice(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '--'
}

function formatChange(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  return value >= 0 ? `+${value.toFixed(2)}%` : `${value.toFixed(2)}%`
}

function formatCount(value: number | null | undefined): number {
  return Number.isFinite(Number(value)) && Number(value) > 0 ? Math.trunc(Number(value)) : 1
}

export function patternReviewRole(row: PatternReviewRow): string {
  if (row.source_type === 'signal_pending') {
    if (row.signal_status === 'confirmed') return '已确认信号'
    if (row.signal_status === 'survived') return '跨日存活信号'
    return '待确认信号'
  }
  return isAiRecommended(row.is_ai_recommended) ? 'AI推荐' : '观察/信号复盘'
}

export function labelCandidateTerm(value: string): string | null {
  const clean = value.trim()
  if (!clean) return null
  const labels: Record<string, string> = {
    mainline: '主线买点',
    trend_breakout: '趋势突破',
    trend_lane_pullback: '趋势回踩',
    sector_strength: '板块强势',
    wyckoff_structure: 'Wyckoff结构',
    sos: 'SOS点火',
    evr: 'EVR放量不跌',
    lps: 'LPS缩量回踩',
    spring: 'Spring震仓',
    Lane: '入选路径',
    可买主线: '主线买点候选',
    主线买点候选: '主线买点候选',
    主线观察: '主线观察',
    过热不追: '过热不追',
  }
  return labels[clean] || clean
}

export function formatPatternReviewLine(row: PatternReviewRow): string {
  const code = normalizeCode(row.code)
  const pricePath = `${formatPrice(row.initial_price)}→${formatPrice(row.current_price)}`
  const lane = [row.candidate_lane || row.signal_key || row.signal_type, row.entry_type || row.candidate_status]
    .map(item => String(item || '').trim())
    .filter(Boolean)
    .map(labelCandidateTerm)
    .filter(Boolean)
    .join('/')
  const mainline = typeof row.mainline_score === 'number' ? `主线${Math.round(row.mainline_score * 100)}` : ''
  const dateLabel = row.source_type === 'signal_pending' ? '信号日' : '入选日'
  return [
    `${code} ${row.name}`,
    patternReviewRole(row),
    `${dateLabel}${row.recommend_date}`,
    `入选${formatCount(row.recommend_count)}次`,
    lane || mainline ? `入选路径${[lane, mainline].filter(Boolean).join(' ')}` : '',
    `${pricePath} ${formatChange(row.change_pct)}`,
  ].filter(Boolean).join(' | ')
}

export function formatPatternReviewDigest(rows: PatternReviewRow[]): string {
  if (rows.length === 0) return PATTERN_REVIEW_EMPTY_MESSAGE
  const lines = rows.map(formatPatternReviewLine)
  return `最近 ${rows.length} 条形态复盘记录：${PATTERN_REVIEW_SCOPE_NOTE}\n\n${lines.join('\n')}`
}

export interface DedupeTrackingRow {
  code: string | number
  recommend_date: string | number
  recommend_count?: number | null
  initial_price?: number | null
  current_price?: number | null
  change_pct?: number | null
  is_ai_recommended?: boolean | number | string | null
  rag_vetoed?: boolean | null
  funnel_score?: number | null
  source_type?: string | null
}

function reviewDateNumber(value: string | number): number {
  const digits = String(value ?? '').replaceAll(/[^\d]/g, '')
  return digits ? Number(digits.slice(0, 8)) : 0
}

export function latestTrackingDates(rows: DedupeTrackingRow[], limit: number): number[] {
  const dates = rows.map((row) => reviewDateNumber(row.recommend_date)).filter((date) => date > 0)
  return [...new Set(dates)].sort((a, b) => b - a).slice(0, limit)
}

export function hasCompleteTrackingWindow(rows: DedupeTrackingRow[], retentionDates: number): boolean {
  const dates = latestTrackingDates(rows, retentionDates + 1)
  const cutoffDate = dates[retentionDates - 1]
  const oldestFetched = rows.at(-1)
  if (!cutoffDate || !oldestFetched || dates.length <= retentionDates) return false
  return reviewDateNumber(oldestFetched.recommend_date) < cutoffDate
}

export function countTrackingOccurrences(rows: DedupeTrackingRow[]): number {
  const occurrences = new Set<string>()
  for (const row of rows) {
    const code = normalizeCode(row.code)
    const date = reviewDateNumber(row.recommend_date)
    if (code && date > 0) occurrences.add(`${date}\u0000${code}`)
  }
  return occurrences.size
}

function positivePrice(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

function trackingSourcePriority(row: DedupeTrackingRow): number {
  return row.source_type === 'signal_pending' ? 1 : 2
}

export function preferTrackingRow(next: DedupeTrackingRow, current: DedupeTrackingRow): boolean {
  const nextDate = reviewDateNumber(next.recommend_date)
  const currentDate = reviewDateNumber(current.recommend_date)
  if (nextDate !== currentDate) return nextDate > currentDate
  const sourceDelta = trackingSourcePriority(next) - trackingSourcePriority(current)
  if (sourceDelta !== 0) return sourceDelta > 0
  const nextAi = isAiRecommended(next.is_ai_recommended)
  const currentAi = isAiRecommended(current.is_ai_recommended)
  if (nextAi !== currentAi) return nextAi
  return (next.funnel_score ?? -Infinity) > (current.funnel_score ?? -Infinity)
}

function stickyInitialPrice(preferred: DedupeTrackingRow, other: DedupeTrackingRow): number | null {
  const trackingPrices = [preferred, other]
    .filter((row) => row.source_type !== 'signal_pending')
    .map((row) => ({ date: reviewDateNumber(row.recommend_date), price: positivePrice(row.initial_price) }))
    .filter((row): row is { date: number; price: number } => row.price != null)
    .sort((a, b) => a.date - b.date)
  if (trackingPrices[0]) return trackingPrices[0].price
  return positivePrice(preferred.initial_price) ?? positivePrice(other.initial_price)
}

function mergeTrackingPrices<T extends DedupeTrackingRow>(preferred: T, other: T): T {
  const initialPrice = stickyInitialPrice(preferred, other)
  const currentPrice = positivePrice(preferred.current_price) ?? positivePrice(other.current_price)
  let changePct = preferred.change_pct ?? null
  if (currentPrice != null && (positivePrice(preferred.current_price) == null || preferred.change_pct == null)) {
    if (initialPrice != null) {
      changePct = Number((((currentPrice - initialPrice) / initialPrice) * 100).toFixed(2))
    } else if (other.change_pct != null) {
      changePct = other.change_pct
    }
  }
  return {
    ...preferred,
    initial_price: initialPrice ?? preferred.initial_price ?? null,
    current_price: currentPrice ?? preferred.current_price ?? null,
    change_pct: changePct,
  }
}

/** 按 code 去重：较新日期优先；同日 tracking 优于 signal_pending；价格字段从 tracking 行补齐。 */
export function dedupeTrackingRows<T extends DedupeTrackingRow>(rows: T[]): T[] {
  const byCode = new Map<string, T>()
  for (const row of rows) {
    const key = normalizeCode(row.code)
    const existing = byCode.get(key)
    if (!existing) {
      byCode.set(key, { ...row, recommend_count: formatCount(row.recommend_count) })
      continue
    }
    const preferred = preferTrackingRow(row, existing) ? row : existing
    const other = preferred === row ? existing : row
    const merged = mergeTrackingPrices(preferred, other)
    byCode.set(key, {
      ...merged,
      is_ai_recommended: isAiRecommended(existing.is_ai_recommended) || isAiRecommended(row.is_ai_recommended),
      rag_vetoed: Boolean(existing.rag_vetoed || row.rag_vetoed),
      recommend_count: Math.max(formatCount(existing.recommend_count), formatCount(row.recommend_count)),
    })
  }
  return [...byCode.values()]
}
