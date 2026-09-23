/** The free TradingView Advanced Chart widget cannot create official drawings and does
 *  not expose price→pixel. An HTML overlay on that iframe drifts when the widget
 *  auto-scales, draws a volume pane, or the user zooms. Levels here are painted on
 *  the same series as the forward-adjusted daily bars, so each price keeps its pixel.
 */
import { useEffect, useRef } from 'react'
import {
  CandlestickSeries,
  createChart,
  HistogramSeries,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type Time,
} from 'lightweight-charts'
import { FibLevelPrimitive } from '@/features/fib-drawing/fib-level-primitive'
import { fibPricePrecision, type FibBar, type FibDrawing } from '@/lib/fib-drawing'
import type { ThemeMode } from '@/lib/preferences'

interface FibPriceChartProps {
  bars: FibBar[]
  drawing: FibDrawing
  theme: ThemeMode
  roomLabel: string
  supplyLabel: string
  onReset: () => void
  onReady: () => void
}

export function FibPriceChart({ bars, drawing, theme, roomLabel, supplyLabel, onReset, onReady }: FibPriceChartProps) {
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    let frame = 0
    let cancelled = false
    onReset()
    const chart = mountFibChart(root, bars, drawing, theme, roomLabel, supplyLabel)
    const stopResize = watchFibChartSize(root, chart)
    frame = window.requestAnimationFrame(() => {
      frame = window.requestAnimationFrame(() => {
        if (!cancelled) onReady()
      })
    })
    return () => {
      cancelled = true
      window.cancelAnimationFrame(frame)
      stopResize()
      chart.remove()
    }
  }, [bars, drawing, theme, roomLabel, supplyLabel, onReset, onReady])

  return <div ref={rootRef} className="h-full min-h-[420px] w-full" />
}

function mountFibChart(
  root: HTMLElement,
  bars: FibBar[],
  drawing: FibDrawing,
  theme: ThemeMode,
  roomLabel: string,
  supplyLabel: string,
) {
  const colors = readFibTheme(theme)
  const chart = createChart(root, {
    width: root.clientWidth,
    height: Math.max(root.clientHeight, 420),
    layout: { background: { color: colors.background }, textColor: colors.mutedText, fontSize: 11 },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.06, bottom: 0.2 } },
    timeScale: { borderColor: colors.border, timeVisible: false, fixLeftEdge: true, fixRightEdge: true },
    crosshair: { mode: 1 },
  })
  const precision = fibPricePrecision(drawing.swingHigh)
  const candle = chart.addSeries(CandlestickSeries, {
    upColor: colors.up,
    downColor: colors.down,
    borderUpColor: colors.up,
    borderDownColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
    priceFormat: { type: 'price', precision, minMove: 10 ** -precision },
  })
  const volume = chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
    lastValueVisible: false,
    priceLineVisible: false,
  })
  chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } })
  candle.setData(toCandles(bars))
  volume.setData(toVolume(bars, colors))
  candle.attachPrimitive(new FibLevelPrimitive(drawing, roomLabel, supplyLabel))
  chart.timeScale().fitContent()
  return chart
}

function watchFibChartSize(root: HTMLElement, chart: IChartApi) {
  const apply = () => chart.applyOptions({ width: root.clientWidth, height: Math.max(root.clientHeight, 420) })
  const observer = new ResizeObserver(apply)
  observer.observe(root)
  apply()
  return () => observer.disconnect()
}

function toCandles(bars: FibBar[]): CandlestickData<Time>[] {
  return bars.map((bar) => ({ time: bar.date as Time, open: bar.open, high: bar.high, low: bar.low, close: bar.close }))
}

function toVolume(bars: FibBar[], colors: FibTheme): HistogramData<Time>[] {
  return bars.map((bar) => ({
    time: bar.date as Time,
    value: bar.volume,
    color: bar.close >= bar.open ? `${colors.up}55` : `${colors.down}55`,
  }))
}

interface FibTheme {
  background: string
  mutedText: string
  border: string
  grid: string
  up: string
  down: string
}

function readFibTheme(mode: ThemeMode): FibTheme {
  const style = getComputedStyle(document.documentElement)
  const color = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback
  return {
    background: color('--color-background', '#ffffff'),
    mutedText: color('--color-muted-foreground', '#6b7194'),
    border: color('--color-border', '#e2e5f1'),
    grid: mode === 'dark' ? '#202938' : '#eef1f6',
    up: color('--color-up', '#ef4444'),
    down: color('--color-down', '#10b981'),
  }
}
