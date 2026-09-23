import { describe, expect, it, vi } from 'vitest'
import type { StockSearchResult } from '../market-search'
import {
  buildFibChartView,
  computeFibDrawing,
  eastmoneySecId,
  fetchPublicDailyBars,
  fibPlotY,
  formatFibPrice,
  parseEastmoneyKlines,
  publicDailyBarsUrl,
  resolveFibTarget,
  tradingViewSymbol,
  tradingViewWidgetOptions,
  type FibBar,
} from '../fib-drawing'

const cn = (code: string, name: string): StockSearchResult => ({
  analysisCode: code,
  symbol: code,
  code,
  name,
  market: 'cn',
  assetType: 'stock',
  aliases: [],
})

describe('tradingViewSymbol', () => {
  it('maps Shanghai and Shenzhen codes the way the public widget lists them', () => {
    expect(tradingViewSymbol('600519')).toBe('SSE:600519')
    expect(tradingViewSymbol('688981')).toBe('SSE:688981')
    expect(tradingViewSymbol('510300')).toBe('SSE:510300')
    expect(tradingViewSymbol('600519.SH')).toBe('SSE:600519')
    expect(tradingViewSymbol('000001')).toBe('SZSE:000001')
    expect(tradingViewSymbol('300750')).toBe('SZSE:300750')
    expect(tradingViewSymbol('159915')).toBe('SZSE:159915')
  })

  it('rejects markets the public widget does not chart', () => {
    expect(tradingViewSymbol('832735')).toBeNull()
    expect(tradingViewSymbol('AAPL')).toBeNull()
    expect(tradingViewSymbol('00700.HK')).toBeNull()
    expect(eastmoneySecId('600519')).toBe('1.600519')
    expect(eastmoneySecId('000001')).toBe('0.000001')
  })
})

describe('tradingViewWidgetOptions', () => {
  it('asks for daily bars the Shanghai end-of-day feed can plot', () => {
    const options = tradingViewWidgetOptions({ symbol: 'SSE:600519', theme: 'light', locale: 'zh_CN' })
    expect(options.interval).toBe('1D')
    expect(options.symbol).toBe('SSE:600519')
    expect('range' in options).toBe(false)
  })
})

describe('computeFibDrawing', () => {
  it('measures levels upward from the window low, with 0.382 as the room floor', () => {
    const older: FibBar[] = [{ date: '2020-01-01', high: 200, low: 1, close: 10 }]
    const window = Array.from({ length: 20 }, (_, index) => ({
      date: `2024-02-${String(index + 1).padStart(2, '0')}`,
      high: index === 10 ? 100 : 80,
      low: index === 3 ? 50 : 60,
      close: 70,
    }))
    const drawing = computeFibDrawing([...older, ...window], 20)
    expect(drawing?.bars).toBe(20)
    expect(drawing?.swingLow).toBe(50)
    expect(drawing?.swingHigh).toBe(100)
    expect(drawing?.lowDate).toBe('2024-02-04')
    expect(drawing?.highDate).toBe('2024-02-11')
    const byRatio = Object.fromEntries((drawing?.levels ?? []).map((level) => [level.ratio, level.price]))
    expect(byRatio[0]).toBe(50)
    expect(byRatio[0.382]).toBeCloseTo(50 + 50 * 0.382)
    expect(byRatio[1]).toBe(100)
    expect(byRatio[1.618]).toBeCloseTo(50 + 50 * 1.618)
  })

  it('refuses a window that is too short or flat', () => {
    expect(computeFibDrawing(risingBars(10), 30)).toBeNull()
    const flat = Array.from({ length: 20 }, (_, i) => ({ date: `2024-01-${String(i + 1).padStart(2, '0')}`, high: 10, low: 10, close: 10 }))
    expect(computeFibDrawing(flat, 30)).toBeNull()
  })
})

describe('eastmoney kline', () => {
  it('parses date, close, high, low from the kline string', () => {
    const bars = parseEastmoneyKlines({
      data: { klines: ['2026-09-23,1255.03,1266.05,1271.50,1252.02,11615'] },
    })
    expect(bars).toEqual([{ date: '2026-09-23', high: 1271.5, low: 1252.02, close: 1266.05 }])
    expect(publicDailyBarsUrl('600519')).toContain('secid=1.600519')
    expect(publicDailyBarsUrl('600519')).toContain('fqt=1')
    expect(publicDailyBarsUrl('600519')).toContain('klt=101')
  })

  it('loads bars through the injected fetcher', async () => {
    const fetcher = vi.fn(async () => ({
      ok: true,
      json: async () => ({ data: { klines: ['2026-09-23,1,2,3,1.5,9'] } }),
    }))
    const bars = await fetchPublicDailyBars('000001', fetcher as unknown as typeof fetch)
    const calls = fetcher.mock.calls as unknown as unknown[][]
    expect(String(calls[0]?.[0])).toContain('secid=0.000001')
    expect(bars[0]?.high).toBe(3)
    const failed = vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) }))
    await expect(fetchPublicDailyBars('600519', failed as unknown as typeof fetch)).rejects.toThrow(/503/)
  })
})

describe('fibPlotY', () => {
  it('places the window high above the window low inside the plot', () => {
    const highY = fibPlotY(100, 50, 100)
    const lowY = fibPlotY(50, 50, 100)
    const floorY = fibPlotY(50 + 50 * 0.382, 50, 100)
    expect(highY).not.toBeNull()
    expect(lowY).not.toBeNull()
    expect(highY!).toBeGreaterThan(0)
    expect(highY!).toBeLessThan(floorY!)
    expect(floorY!).toBeLessThan(lowY!)
    expect(lowY!).toBeLessThan(1)
  })
})

describe('resolveFibTarget', () => {
  it('keeps a selected Shanghai code and drops unsupported markets', async () => {
    await expect(resolveFibTarget('600519', cn('600519', '贵州茅台'))).resolves.toEqual({
      code: '600519',
      name: '贵州茅台',
      tvSymbol: 'SSE:600519',
    })
    const us: StockSearchResult = {
      analysisCode: 'AAPL.US',
      symbol: 'AAPL.US',
      code: 'AAPL',
      name: 'Apple',
      market: 'us',
      assetType: 'stock',
      aliases: [],
    }
    await expect(resolveFibTarget('AAPL.US', us)).resolves.toBeNull()
    await expect(resolveFibTarget('832735', cn('832735', '北交示例'))).resolves.toBeNull()
  })
})

describe('buildFibChartView', () => {
  it('returns a drawing when the public bars cover the lookback', async () => {
    const fetcher = vi.fn(async () => ({
      ok: true,
      json: async () => ({ data: { klines: klineRows(25) } }),
    }))
    const result = await buildFibChartView('000001', cn('000001', '平安银行'), fetcher as unknown as typeof fetch)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.view.tvSymbol).toBe('SZSE:000001')
    expect(result.view.drawing.levels.map((level) => level.ratio)).toContain(0.382)
    expect(formatFibPrice(result.view.drawing.swingHigh)).toMatch(/^\d+\.\d+$/)
  })
})

function risingBars(count: number): FibBar[] {
  return Array.from({ length: count }, (_, index) => {
    const low = 10 + index
    return { date: `2024-03-${String(index + 1).padStart(2, '0')}`, high: low + 1, low, close: low + 0.5 }
  })
}

function klineRows(count: number): string[] {
  return risingBars(count).map((bar) => `${bar.date},${bar.low},${bar.close},${bar.high},${bar.low},100`)
}
