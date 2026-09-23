import { isCnSymbol } from '@wyckoff/shared'
import { matchesSelectedQuery } from '@/components/stock-search-box'
import { resolveStockQuery, type StockSearchResult } from '@/lib/market-search'

/** Same window as `core/channel_geometry.py` DEFAULT_WINDOW. Levels are measured
 *  upward from the window low, matching `_fib_room` / FIB_EXTENSION (赚钱空间).
 *  This is a display grid, not a funnel gate. */
export const FIB_LOOKBACK = 120
export const FIB_MIN_BARS = 20

/** Free-widget plot is not a price scale we can read. These insets keep lines off
 *  the symbol header, time axis, and TradingView attribution row. */
export const FIB_PLOT_INSET = { top: 0.09, right: 0.1, bottom: 0.14, left: 0.02 }
export const FIB_SCALE_PAD = 0.05

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
  high: number
  low: number
  close: number
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
}

export type FibChartResult =
  | { ok: true; view: FibChartView }
  | { ok: false; reason: 'unsupported' | 'short' | 'fetch' }

const EASTMONEY_KLINE = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'

export function normalizeAshareCode(raw: string): string {
  return raw.trim().toUpperCase().replace(/\.(SH|SZ|BJ)$/, '')
}

/** TradingView advanced-chart symbols. Beijing listings are absent from that widget. */
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
    lmt: '250',
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

export function computeFibDrawing(bars: FibBar[], lookback = FIB_LOOKBACK): FibDrawing | null {
  const slice = bars.slice(-lookback)
  if (slice.length < FIB_MIN_BARS) return null
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

/** Fraction of the chart box height. 0 is the top. Ratios above 1 sit off-scale. */
export function fibPlotY(price: number, swingLow: number, swingHigh: number): number | null {
  const span = swingHigh - swingLow
  if (!(span > 0)) return null
  const top = swingHigh + span * FIB_SCALE_PAD
  const bottom = swingLow - span * FIB_SCALE_PAD
  const t = (top - price) / (top - bottom)
  const plot = 1 - FIB_PLOT_INSET.top - FIB_PLOT_INSET.bottom
  return FIB_PLOT_INSET.top + t * plot
}

export function formatFibPrice(price: number): string {
  return price.toFixed(price >= 100 ? 2 : 3)
}

export function tradingViewWidgetOptions(input: { symbol: string; theme: 'light' | 'dark'; locale: 'zh_CN' | 'en' }) {
  return {
    autosize: true,
    symbol: input.symbol,
    // The embed treats "D" as unknown and opens 120-minute bars. A "6M" range does the
    // same. Shanghai end-of-day symbols only plot D/W/M, so either choice leaves a blank chart.
    interval: '1D',
    timezone: 'Asia/Shanghai',
    theme: input.theme,
    style: '1',
    locale: input.locale,
    hide_side_toolbar: true,
    allow_symbol_change: false,
    save_image: false,
    calendar: false,
    withdateranges: false,
    support_host: 'https://www.tradingview.com',
  }
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
  fetcher: typeof fetch = globalThis.fetch,
): Promise<FibChartResult> {
  const target = await resolveFibTarget(raw, selected)
  if (!target) return { ok: false, reason: 'unsupported' }
  try {
    const bars = await fetchPublicDailyBars(target.code, fetcher)
    const drawing = computeFibDrawing(bars)
    if (!drawing) return { ok: false, reason: 'short' }
    return { ok: true, view: { ...target, drawing } }
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
  const [date = '', , closeRaw = '', highRaw = '', lowRaw = ''] = row.split(',')
  const high = Number(highRaw)
  const low = Number(lowRaw)
  const close = Number(closeRaw)
  if (!date || !(high > 0) || !(low > 0) || !(close > 0) || high < low) return null
  return { date, high, low, close }
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
