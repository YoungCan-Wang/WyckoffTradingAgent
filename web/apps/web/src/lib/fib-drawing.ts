import { isCnSymbol } from '@wyckoff/shared'
import { matchesSelectedQuery } from '@/components/stock-search-box'
import { resolveStockQuery, type StockSearchResult } from '@/lib/market-search'

/** Same window as `core/channel_geometry.py` DEFAULT_WINDOW. Levels are measured
 *  upward from the window low, matching `_fib_room` / FIB_EXTENSION (赚钱空间).
 *  This is a display grid, not a funnel gate. */
export const FIB_LOOKBACK = 120
export const FIB_MIN_BARS = 20
/** 3 calendar years of A-share sessions, plus a short buffer. */
export const FIB_HISTORY_BARS = 1200

export const FIB_PRESETS = ['d120', 'month', 'm6', 'y1', 'y3'] as const
export type FibPreset = (typeof FIB_PRESETS)[number]

export const FIB_LEVEL_SPECS = [
  { ratio: 0, label: '0%', role: 'low' },
  { ratio: 0.236, label: '23.6%', role: 'retracement' },
  { ratio: 0.382, label: '38.2%', role: 'roomLower' },
  { ratio: 0.5, label: '50%', role: 'mid' },
  { ratio: 0.618, label: '61.8%', role: 'retracement' },
  { ratio: 0.786, label: '78.6%', role: 'supply' },
  { ratio: 1, label: '100%', role: 'high' },
  { ratio: 1.618, label: '161.8%', role: 'extension' },
] as const

export type FibRole = (typeof FIB_LEVEL_SPECS)[number]['role']

export interface FibBar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface FibLevel {
  ratio: number
  label: string
  role: FibRole
  price: number
}

export interface FibDrawing {
  swingLow: number
  swingHigh: number
  lowDate: string
  highDate: string
  bars: number
  levels: FibLevel[]
}

export interface FibTarget {
  code: string
  name: string
  tvSymbol: string
}

export interface FibChartView extends FibTarget {
  drawing: FibDrawing
  /** The same window the levels were measured on, so the candles and the lines share one scale. */
  bars: FibBar[]
}

export type FibChartResult =
  | { ok: true; view: FibChartView }
  | { ok: false; reason: 'unsupported' | 'short' | 'fetch' }

const EASTMONEY_KLINE = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'

export function normalizeAshareCode(raw: string): string {
  return raw.trim().toUpperCase().replace(/\.(SH|SZ|BJ)$/, '')
}

/** Shanghai / Shenzhen prefix for the legend. Beijing, HK, and US stay off this public chart. */
export function tradingViewSymbol(raw: string): string | null {
  const code = normalizeAshareCode(raw)
  if (!isCnSymbol(code)) return null
  if (code.startsWith('6') || code.startsWith('5')) return `SSE:${code}`
  if (code.startsWith('0') || code.startsWith('1') || code.startsWith('2') || code.startsWith('3')) return `SZSE:${code}`
  return null
}

export function eastmoneySecId(raw: string): string | null {
  const code = normalizeAshareCode(raw)
  if (!isCnSymbol(code)) return null
  if (code.startsWith('6') || code.startsWith('5')) return `1.${code}`
  return `0.${code}`
}

export function publicDailyBarsUrl(raw: string): string | null {
  const secid = eastmoneySecId(raw)
  if (!secid) return null
  const params = new URLSearchParams({
    secid,
    fields1: 'f1,f2,f3,f4,f5,f6',
    fields2: 'f51,f52,f53,f54,f55,f56',
    klt: '101',
    fqt: '1',
    end: '20500101',
    lmt: String(FIB_HISTORY_BARS),
  })
  return `${EASTMONEY_KLINE}?${params}`
}

export function parseEastmoneyKlines(payload: unknown): FibBar[] {
  const klines = readKlines(payload)
  const bars: FibBar[] = []
  for (const row of klines) {
    const bar = parseKlineRow(String(row))
    if (bar) bars.push(bar)
  }
  bars.sort((a, b) => a.date.localeCompare(b.date))
  return bars
}

export function lookbackStart(preset: FibPreset, now: Date): string | null {
  if (preset === 'd120') return null
  if (preset === 'month') return formatDay(new Date(now.getFullYear(), now.getMonth(), 1))
  const months = preset === 'm6' ? -6 : preset === 'y1' ? -12 : -36
  return formatDay(addMonths(now, months))
}

export function barsForPreset(bars: FibBar[], preset: FibPreset, now = new Date()): FibBar[] {
  if (preset === 'd120') return bars.slice(-FIB_LOOKBACK)
  const start = lookbackStart(preset, now) ?? ''
  return bars.filter((bar) => bar.date >= start)
}

export function computeFibDrawing(bars: FibBar[], preset: FibPreset = 'd120', now = new Date()): FibDrawing | null {
  const slice = barsForPreset(bars, preset, now)
  if (slice.length < presetMinBars(preset)) return null
  const extreme = windowExtreme(slice)
  if (!extreme) return null
  const span = extreme.swingHigh - extreme.swingLow
  return {
    ...extreme,
    bars: slice.length,
    levels: FIB_LEVEL_SPECS.map((spec) => ({
      ...spec,
      price: extreme.swingLow + span * spec.ratio,
    })),
  }
}

export function formatFibPrice(price: number): string {
  return price.toFixed(fibPricePrecision(price))
}

export function fibPricePrecision(price: number): number {
  return price >= 100 ? 2 : 3
}

export async function resolveFibTarget(raw: string, selected: StockSearchResult | null): Promise<FibTarget | null> {
  const stock = matchesSelectedQuery(raw, selected, 'analysis') ? selected : await resolveStockQuery(raw)
  const code = normalizeAshareCode(stock?.analysisCode || raw)
  const tvSymbol = tradingViewSymbol(code)
  if (!tvSymbol) return null
  return { code, name: stock?.name || '', tvSymbol }
}

export async function buildFibChartView(
  raw: string,
  selected: StockSearchResult | null,
  preset: FibPreset = 'd120',
  fetcher: typeof fetch = globalThis.fetch,
  now = new Date(),
): Promise<FibChartResult> {
  const target = await resolveFibTarget(raw, selected)
  if (!target) return { ok: false, reason: 'unsupported' }
  try {
    const bars = await fetchPublicDailyBars(target.code, fetcher)
    const drawing = computeFibDrawing(bars, preset, now)
    if (!drawing) return { ok: false, reason: 'short' }
    return { ok: true, view: { ...target, drawing, bars: barsForPreset(bars, preset, now) } }
  } catch {
    return { ok: false, reason: 'fetch' }
  }
}

export async function fetchPublicDailyBars(code: string, fetcher: typeof fetch = globalThis.fetch): Promise<FibBar[]> {
  const url = publicDailyBarsUrl(code)
  if (!url) return []
  const resp = await fetcher(url)
  if (!resp.ok) throw new Error(`eastmoney kline ${resp.status}`)
  return parseEastmoneyKlines(await resp.json())
}

function readKlines(payload: unknown): unknown[] {
  if (!payload || typeof payload !== 'object') return []
  const data = (payload as { data?: { klines?: unknown } }).data
  return Array.isArray(data?.klines) ? data.klines : []
}

function parseKlineRow(row: string): FibBar | null {
  const [date = '', openRaw = '', closeRaw = '', highRaw = '', lowRaw = '', volumeRaw = ''] = row.split(',')
  const open = Number(openRaw)
  const high = Number(highRaw)
  const low = Number(lowRaw)
  const close = Number(closeRaw)
  const volume = Number(volumeRaw)
  if (!date || !(open > 0) || !(high > 0) || !(low > 0) || !(close > 0) || high < low) return null
  return { date, open, high, low, close, volume: Number.isFinite(volume) ? volume : 0 }
}

function presetMinBars(preset: FibPreset): number {
  return preset === 'month' ? 1 : FIB_MIN_BARS
}

function addMonths(date: Date, months: number): Date {
  const next = new Date(date.getFullYear(), date.getMonth() + months, 1)
  const last = new Date(next.getFullYear(), next.getMonth() + 1, 0).getDate()
  next.setDate(Math.min(date.getDate(), last))
  return next
}

function formatDay(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

function windowExtreme(slice: FibBar[]): Omit<FibDrawing, 'bars' | 'levels'> | null {
  let swingHigh = -Infinity
  let swingLow = Infinity
  let highDate = ''
  let lowDate = ''
  for (const bar of slice) {
    if (bar.high >= swingHigh) {
      swingHigh = bar.high
      highDate = bar.date
    }
    if (bar.low <= swingLow) {
      swingLow = bar.low
      lowDate = bar.date
    }
  }
  if (!(swingHigh > swingLow) || swingLow <= 0) return null
  return { swingLow, swingHigh, lowDate, highDate }
}
