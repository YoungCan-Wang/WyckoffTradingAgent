import { useState, useEffect, useMemo, useRef } from 'react'
import { watchChartResize } from '@/lib/chart-resize'
import { formatSignedPercent } from '@/lib/format'
import { avg, rsi as calcRSI, macd as calcMACD, bollinger as calcBollinger } from '@/lib/math'
import { deriveForecastSeries, deriveWyckoffZones, formatChartPlanNotes, type KlineRow, type WyckoffChartPlan } from '@wyckoff/shared'
import { WyckoffZonePrimitive } from '@/components/wyckoff-zone-primitive'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts'

interface WyckoffMarkerInput {
  date: string
  type: 'spring' | 'sos' | 'lps' | 'evr'
  label: string
  position: 'aboveBar' | 'belowBar'
}

interface NewsMarkerInput {
  date: string
  sentiment: 'bullish' | 'bearish' | 'mixed' | 'unknown'
  label: string
}

interface KlineChartProps {
  data: KlineRow[]
  height?: number
  wyckoffMarkers?: WyckoffMarkerInput[]
  newsMarkers?: NewsMarkerInput[]
  tradingRange?: { support: number; resistance: number }
  stage?: string
  showIndicators?: boolean
  /** 模型给的威科夫作图计划:阶段、事件、推演。给了就画长周期均线、底色、预测线。 */
  chartPlan?: WyckoffChartPlan | null
  onBarClick?: (date: string) => void
}

interface StructureSnapshot {
  changePct: number
  latestClose: number
  ma20: number
  ma50: number
  support: number
  resistance: number
  volumeRatio: number
  phase: string
  tone: 'strong' | 'watch' | 'weak'
}

interface ChartRefs {
  chart: IChartApi
  candle: ISeriesApi<'Candlestick'>
  ma5: ISeriesApi<'Line'>
  ma20: ISeriesApi<'Line'>
  ma50: ISeriesApi<'Line'>
  ma200: ISeriesApi<'Line'>
  volume: ISeriesApi<'Histogram'>
  markers: ISeriesMarkersPluginApi<Time> | null
}

type ChartTheme = ReturnType<typeof readChartTheme>

export function KlineChart({ data, height = 400, wyckoffMarkers, newsMarkers, tradingRange, stage, showIndicators = false, chartPlan, onBarClick }: KlineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRefs = useRef<ChartRefs | null>(null)
  const themeRef = useRef(readChartTheme())
  const structure = useMemo(() => buildStructureSnapshot(data, tradingRange, stage), [data, tradingRange, stage])
  const [indicators, setIndicators] = useState({ boll: false, rsi: false, macd: false })
  const planMarkers = useMemo(() => (chartPlan ? toPlanMarkers(chartPlan) : undefined), [chartPlan])
  const planNotes = useMemo(() => (chartPlan ? formatChartPlanNotes(chartPlan) : []), [chartPlan])

  useChartInit(containerRef, chartRefs, themeRef, height)
  useChartData(chartRefs, themeRef, data, planMarkers ?? wyckoffMarkers, newsMarkers, tradingRange)
  useChartSelection(chartRefs, onBarClick)
  useBollingerOverlay(chartRefs, data, indicators.boll)
  useChartPlanOverlay(chartRefs, data, chartPlan)

  const closes = useMemo(() => data.map((d) => d.close), [data])
  const dates = useMemo(() => data.map((d) => d.date), [data])

  return (
    <div className="space-y-3">
      {structure && <StructureMetrics structure={structure} />}
      <div
        ref={containerRef}
        className={`w-full overflow-hidden rounded-lg border border-border bg-background ${onBarClick ? 'cursor-crosshair' : ''}`}
        style={{ height }}
      />
      {showIndicators && <IndicatorBar indicators={indicators} setIndicators={setIndicators} />}
      {indicators.rsi && <RSISubChart closes={closes} dates={dates} />}
      {indicators.macd && <MACDSubChart closes={closes} dates={dates} />}
      <ChartLegend boll={indicators.boll} wyckoffMarkers={!!wyckoffMarkers || !!planMarkers} newsMarkers={!!newsMarkers?.length} chartPlan={!!chartPlan} />
      {planNotes.length > 0 && <ChartPlanNotes notes={planNotes} />}
    </div>
  )
}

/**
 * 图上只挂术语,理由列在图下。
 *
 * 术语加长句一起塞进 marker,几个事件挨在一起就互相压住,谁也读不清。宁可
 * 图上少显示一个字,也不牺牲整张图的可读性。
 */
function ChartPlanNotes({ notes }: { notes: string[] }) {
  return (
    <ul className="space-y-1 rounded-lg border border-border bg-muted/20 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
      {notes.map((note) => <li key={note}>{note}</li>)}
    </ul>
  )
}

function useChartSelection(chartRefs: React.MutableRefObject<ChartRefs | null>, onBarClick?: (date: string) => void) {
  useEffect(() => {
    const chart = chartRefs.current?.chart
    if (!chart || !onBarClick) return
    const handleClick = (param: { time?: Time }) => {
      if (typeof param.time === 'string') onBarClick(param.time)
    }
    chart.subscribeClick(handleClick)
    return () => chart.unsubscribeClick(handleClick)
  }, [chartRefs, onBarClick])
}

function useChartInit(
  containerRef: React.RefObject<HTMLDivElement | null>,
  chartRefs: React.MutableRefObject<ChartRefs | null>,
  themeRef: React.MutableRefObject<ChartTheme>,
  height: number,
) {
  useEffect(() => {
    if (!containerRef.current) return
    themeRef.current = readChartTheme()
    const theme = themeRef.current
    const chart = createChart(containerRef.current, {
      height,
      layout: { background: { color: theme.background }, textColor: theme.mutedText, fontSize: 11 },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border, timeVisible: false },
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: theme.up, downColor: theme.down,
      borderUpColor: theme.up, borderDownColor: theme.down,
      wickUpColor: theme.up, wickDownColor: theme.down,
    })
    const maOpts = { priceLineVisible: false, lastValueVisible: false } as const
    const ma5 = chart.addSeries(LineSeries, { ...maOpts, color: '#f59e0b', lineWidth: 1 })
    const ma20 = chart.addSeries(LineSeries, { ...maOpts, color: '#2563eb', lineWidth: 2 })
    const ma50 = chart.addSeries(LineSeries, { ...maOpts, color: '#7c3aed', lineWidth: 2, lineStyle: LineStyle.Dashed })
    const ma200 = chart.addSeries(LineSeries, { ...maOpts, color: '#dc2626', lineWidth: 2, lineStyle: LineStyle.Dashed })
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } })
    chartRefs.current = { chart, candle, ma5, ma20, ma50, ma200, volume, markers: null }
    const stopResize = watchChartResize(containerRef.current, chart)
    return () => { stopResize(); chartRefs.current?.markers?.detach(); chart.remove(); chartRefs.current = null }
  }, [containerRef, chartRefs, themeRef, height])
}

function useChartData(
  chartRefs: React.MutableRefObject<ChartRefs | null>,
  themeRef: React.MutableRefObject<ChartTheme>,
  data: KlineRow[],
  wyckoffMarkers: WyckoffMarkerInput[] | undefined,
  newsMarkers: NewsMarkerInput[] | undefined,
  tradingRange: { support: number; resistance: number } | undefined,
) {
  useEffect(() => {
    const refs = chartRefs.current
    if (!refs || data.length === 0) return
    const theme = themeRef.current
    const candles: CandlestickData<Time>[] = data.map((d) => ({ time: d.date as Time, open: d.open, high: d.high, low: d.low, close: d.close }))
    const volumes: HistogramData<Time>[] = data.map((d) => ({
      time: d.date as Time, value: d.volume, color: d.close >= d.open ? `${theme.up}4d` : `${theme.down}4d`,
    }))
    refs.candle.setData(candles)
    refs.ma5.setData(movingAverage(data, 5))
    refs.ma20.setData(movingAverage(data, 20))
    refs.ma50.setData(movingAverage(data, 50))
    // 不足 200 根时 movingAverage 返回空数组,线自动不画 —— 不用半截数据凑一条。
    refs.ma200.setData(movingAverage(data, 200))
    refs.volume.setData(volumes)
    const markers = [...(wyckoffMarkers ? toSeriesMarkers(wyckoffMarkers) : buildMarkers(data)), ...toNewsMarkers(newsMarkers)]
    if (refs.markers) refs.markers.setMarkers(markers)
    else refs.markers = createSeriesMarkers(refs.candle, markers)
    refs.candle.priceLines().forEach((line) => refs.candle.removePriceLine(line))
    addPriceLines(refs.candle, tradingRange ?? buildPriceLevels(data), theme)
    refs.chart.timeScale().fitContent()
  }, [chartRefs, themeRef, data, wyckoffMarkers, newsMarkers, tradingRange])
}

function useBollingerOverlay(chartRefs: React.MutableRefObject<ChartRefs | null>, data: KlineRow[], active: boolean) {
  useEffect(() => {
    const refs = chartRefs.current
    if (!refs || data.length === 0) return
    const bollRefs: ISeriesApi<'Line'>[] = []
    if (active) {
      const boll = calcBollinger(data.map((d) => d.close))
      const lineOpt = { priceLineVisible: false, lastValueVisible: false, lineWidth: 1 as const, lineStyle: LineStyle.Dashed }
      const upper = refs.chart.addSeries(LineSeries, { ...lineOpt, color: '#94a3b8' })
      const mid = refs.chart.addSeries(LineSeries, { ...lineOpt, color: '#94a3b8', lineStyle: LineStyle.Dotted })
      const lower = refs.chart.addSeries(LineSeries, { ...lineOpt, color: '#94a3b8' })
      const toLine = (vals: (number | null)[]) => vals.map((v, i) => v != null ? { time: data[i]!.date as Time, value: v } : null).filter(Boolean) as LineData<Time>[]
      upper.setData(toLine(boll.upper)); mid.setData(toLine(boll.middle)); lower.setData(toLine(boll.lower))
      bollRefs.push(upper, mid, lower)
    }
    return () => { bollRefs.forEach((s) => refs.chart.removeSeries(s)) }
  }, [chartRefs, data, active])
}

/**
 * 阶段底色、阶段分割线、30 日推演线。
 *
 * 每次 plan 变了整套重建 —— 这三样都是整体一致的东西,增量更新容易留下上一轮的
 * 残留线条。重建的代价是几个 addSeries,可以接受。
 */
function useChartPlanOverlay(
  chartRefs: React.MutableRefObject<ChartRefs | null>,
  data: KlineRow[],
  plan: WyckoffChartPlan | null | undefined,
) {
  useEffect(() => {
    const refs = chartRefs.current
    if (!refs || data.length === 0 || !plan) return
    const added: ISeriesApi<'Line'>[] = []
    const zones = deriveWyckoffZones(plan, data)
    const zonePrimitive = zones.length > 0 ? new WyckoffZonePrimitive(zones) : null
    if (zonePrimitive) refs.candle.attachPrimitive(zonePrimitive)

    // 阶段分割线画成竖线:同一天两个点,一个贴顶一个贴底。用独立 series 而不是
    // priceLine —— priceLine 只能横着画。
    const bounds = priceBounds(data)
    for (const phase of plan.phases) {
      const divider = refs.chart.addSeries(LineSeries, {
        color: '#94a3b8', lineWidth: 1, lineStyle: LineStyle.Dotted,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      })
      divider.setData([
        { time: phase.startDate as Time, value: bounds.low },
        { time: phase.startDate as Time, value: bounds.high },
      ])
      added.push(divider)
    }

    const forecast = deriveForecastSeries(plan.forecast, data)
    if (forecast.length > 1) {
      const line = refs.chart.addSeries(LineSeries, {
        color: '#f97316', lineWidth: 2, lineStyle: LineStyle.Dashed,
        priceLineVisible: false, lastValueVisible: true, title: '推演',
      })
      line.setData(forecast.map((point) => ({ time: point.date as Time, value: point.value })))
      added.push(line)
    }
    refs.chart.timeScale().fitContent()

    return () => {
      if (zonePrimitive) refs.candle.detachPrimitive(zonePrimitive)
      added.forEach((series) => refs.chart.removeSeries(series))
    }
  }, [chartRefs, data, plan])
}

function priceBounds(data: KlineRow[]): { low: number; high: number } {
  return {
    low: Math.min(...data.map((row) => row.low)),
    high: Math.max(...data.map((row) => row.high)),
  }
}

/** 图上只挂术语原文,理由交给 ChartPlanNotes —— 长句挤在 K 线上谁也读不清。 */
function toPlanMarkers(plan: WyckoffChartPlan): WyckoffMarkerInput[] {
  return plan.events.map((event) => ({
    date: event.date,
    type: planMarkerType(event.term),
    label: event.term,
    position: event.side === 'above' ? 'aboveBar' : 'belowBar',
  }))
}

function planMarkerType(term: string): WyckoffMarkerInput['type'] {
  const normalized = term.toLowerCase()
  if (normalized.includes('spring') || normalized.includes('sc')) return 'spring'
  if (normalized.includes('sos') || normalized.includes('jac')) return 'sos'
  if (normalized.includes('lps') || normalized.includes('st')) return 'lps'
  return 'evr'
}

function StructureMetrics({ structure }: { structure: StructureSnapshot }) {
  return (
    <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
      <Metric label="最新收盘" value={`${formatPrice(structure.latestClose)} (${formatSignedPercent(structure.changePct)})`} tone={structure.tone} />
      <Metric label="结构状态" value={structure.phase} tone={structure.tone} />
      <Metric label="支撑 / 压力" value={`${formatPrice(structure.support)} / ${formatPrice(structure.resistance)}`} />
      <Metric label="量能 / 均线" value={`${structure.volumeRatio.toFixed(1)}x · MA20 ${formatPrice(structure.ma20)} · MA50 ${formatPrice(structure.ma50)}`} />
    </div>
  )
}

function IndicatorBar({ indicators, setIndicators }: { indicators: { boll: boolean; rsi: boolean; macd: boolean }; setIndicators: React.Dispatch<React.SetStateAction<{ boll: boolean; rsi: boolean; macd: boolean }>> }) {
  return (
    <div className="flex flex-wrap gap-2 text-[11px]">
      <IndicatorToggle label="BOLL" active={indicators.boll} onToggle={() => setIndicators((p) => ({ ...p, boll: !p.boll }))} />
      <IndicatorToggle label="RSI" active={indicators.rsi} onToggle={() => setIndicators((p) => ({ ...p, rsi: !p.rsi }))} />
      <IndicatorToggle label="MACD" active={indicators.macd} onToggle={() => setIndicators((p) => ({ ...p, macd: !p.macd }))} />
    </div>
  )
}

function ChartLegend({ boll, wyckoffMarkers, newsMarkers, chartPlan = false }: { boll: boolean; wyckoffMarkers: boolean; newsMarkers: boolean; chartPlan?: boolean }) {
  return (
    <div className="flex flex-wrap gap-3 text-[11px] text-muted-foreground">
      <Legend color="#f59e0b" label="MA5" />
      <Legend color="#2563eb" label="MA20" />
      <Legend color="#7c3aed" label="MA50" />
      <Legend color="#dc2626" label="MA200" />
      {chartPlan && (
        <>
          <Legend color="#f97316" label="30日推演" />
          <Legend color="#2563eb" label="吸筹区" />
          <Legend color="#ef4444" label="派发区" />
        </>
      )}
      {boll && <Legend color="#94a3b8" label="BOLL" />}
      {newsMarkers && <Legend color="#0f766e" label="新闻" />}
      {wyckoffMarkers ? (
        <>
          <Legend color="#f59e0b" label="Spring" />
          <Legend color="#ef4444" label="SOS" />
          <Legend color="#2563eb" label="LPS" />
          <Legend color="#7c3aed" label="EVR" />
        </>
      ) : (
        <>
          <Legend color="#ef4444" label="SOS/突破" />
          <Legend color="#2563eb" label="测试" />
          <Legend color="#10b981" label="供应风险" />
        </>
      )}
    </div>
  )
}

function IndicatorToggle({ label, active, onToggle }: { label: string; active: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`rounded-md border px-2 py-1 text-[11px] font-medium transition-colors ${active ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:bg-muted/50'}`}
    >
      {label}
    </button>
  )
}

function RSISubChart({ closes, dates }: { closes: number[]; dates: string[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!containerRef.current || closes.length === 0) return
    const theme = readChartTheme()
    const chart = createChart(containerRef.current, {
      height: 120,
      layout: { background: { color: theme.background }, textColor: theme.mutedText, fontSize: 10 },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border, visible: false },
    })
    const line = chart.addSeries(LineSeries, { color: '#8b5cf6', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, title: 'RSI' })
    const rsiResult = calcRSI(closes)
    const points: LineData<Time>[] = []
    for (let i = 0; i < rsiResult.values.length; i++) {
      if (rsiResult.values[i] != null) points.push({ time: dates[i]! as Time, value: rsiResult.values[i]! })
    }
    line.setData(points)
    line.createPriceLine({ price: 70, color: '#ef4444', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false })
    line.createPriceLine({ price: 30, color: '#10b981', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false })
    chart.timeScale().fitContent()
    const stopResize = watchChartResize(containerRef.current, chart)
    return () => { stopResize(); chart.remove() }
  }, [closes, dates])
  return <div ref={containerRef} className="w-full overflow-hidden rounded-lg border border-border bg-background" />
}

function MACDSubChart({ closes, dates }: { closes: number[]; dates: string[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!containerRef.current || closes.length === 0) return
    const theme = readChartTheme()
    const chart = createChart(containerRef.current, {
      height: 120,
      layout: { background: { color: theme.background }, textColor: theme.mutedText, fontSize: 10 },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border, visible: false },
    })
    const macdResult = calcMACD(closes)
    const macdLine = chart.addSeries(LineSeries, { color: '#2563eb', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: 'MACD' })
    const sigLine = chart.addSeries(LineSeries, { color: '#f59e0b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: 'Signal' })
    const hist = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false })

    const toLine = (vals: (number | null)[]) => vals.map((v, i) => v != null ? { time: dates[i]! as Time, value: v } : null).filter(Boolean) as LineData<Time>[]
    macdLine.setData(toLine(macdResult.macd))
    sigLine.setData(toLine(macdResult.signal))
    hist.setData(
      macdResult.histogram
        .map((v, i) => v != null ? { time: dates[i]! as Time, value: v, color: v >= 0 ? '#ef444480' : '#10b98180' } : null)
        .filter(Boolean) as HistogramData<Time>[],
    )
    chart.timeScale().fitContent()
    const stopResize = watchChartResize(containerRef.current, chart)
    return () => { stopResize(); chart.remove() }
  }, [closes, dates])
  return <div ref={containerRef} className="w-full overflow-hidden rounded-lg border border-border bg-background" />
}

// Chinese market convention: red = bullish/up, green = bearish/down (opposite of Western markets)
function Metric({ label, value, tone = 'watch' }: { label: string; value: string; tone?: StructureSnapshot['tone'] }) {
  const toneClass = tone === 'strong'
    ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200'
    : tone === 'weak'
      ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200'
      : 'border-border bg-muted/40 text-foreground'

  return (
    <div className={`rounded-lg border px-3 py-2 ${toneClass}`}>
      <div className="mb-1 text-[11px] text-muted-foreground">{label}</div>
      <div className="font-medium">{value}</div>
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
      {label}
    </span>
  )
}

function addPriceLines(series: ISeriesApi<'Candlestick'>, levels: { support: number; resistance: number }, theme: ReturnType<typeof readChartTheme>) {
  if (levels.support > 0) {
    series.createPriceLine({ price: levels.support, color: theme.support, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '支撑' })
  }
  if (levels.resistance > 0) {
    series.createPriceLine({ price: levels.resistance, color: theme.resistance, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '压力' })
  }
}

function readChartTheme() {
  const style = getComputedStyle(document.documentElement)
  const color = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback
  return {
    background: color('--color-background', '#ffffff'),
    mutedText: color('--color-muted-foreground', '#6b7194'),
    border: color('--color-border', '#e2e5f1'),
    grid: document.documentElement.classList.contains('dark') ? '#202938' : '#eef1f6',
    up: color('--color-up', '#ef4444'),
    down: color('--color-down', '#10b981'),
    support: '#2563eb',
    resistance: '#f59e0b',
  }
}

function movingAverage(data: KlineRow[], period: number): LineData<Time>[] {
  if (data.length < period) return []
  const points: LineData<Time>[] = []
  let sum = 0
  for (let i = 0; i < period; i++) sum += data[i]!.close
  points.push({ time: data[period - 1]!.date as Time, value: sum / period })
  for (let i = period; i < data.length; i++) {
    sum += data[i]!.close - data[i - period]!.close
    points.push({ time: data[i]!.date as Time, value: sum / period })
  }
  return points
}

const MARKER_STYLES: Record<string, { shape: 'arrowUp' | 'arrowDown' | 'circle'; color: string }> = {
  spring: { shape: 'arrowUp', color: '#f59e0b' },
  sos: { shape: 'arrowUp', color: '#ef4444' },
  lps: { shape: 'circle', color: '#2563eb' },
  evr: { shape: 'arrowDown', color: '#7c3aed' },
}

function toSeriesMarkers(markers: WyckoffMarkerInput[]): SeriesMarker<Time>[] {
  return markers.map((m) => {
    const style = MARKER_STYLES[m.type] ?? { shape: 'circle' as const, color: '#6b7280' }
    return { time: m.date as Time, position: m.position, shape: style.shape, color: style.color, text: m.label, size: 1.2 }
  })
}

function toNewsMarkers(markers: NewsMarkerInput[] | undefined): SeriesMarker<Time>[] {
  return (markers || []).map((marker) => ({
    time: marker.date as Time,
    position: 'aboveBar',
    shape: 'square',
    color: marker.sentiment === 'bullish' ? '#ef4444' : marker.sentiment === 'bearish' ? '#10b981' : '#0f766e',
    text: marker.label,
    size: 1,
  }))
}

function buildMarkers(data: KlineRow[]): SeriesMarker<Time>[] {
  const markers: SeriesMarker<Time>[] = []
  const start = Math.max(20, data.length - 150)

  const volSum = new Float64Array(data.length + 1)
  const highMax = new Float64Array(data.length)
  for (let i = 0; i < data.length; i++) volSum[i + 1] = volSum[i]! + data[i]!.volume

  for (let i = start; i < data.length; i++) {
    const lo = Math.max(0, i - 20)
    const len = i - lo
    if (len < 10) continue
    const volume20 = (volSum[i]! - volSum[lo]!) / len

    if (!highMax[i - 1]) {
      let mx = 0
      for (let j = lo; j < i; j++) mx = Math.max(mx, data[j]!.high)
      highMax[i] = mx
    }
    const prevHigh = Math.max(highMax[i - 1] || 0, data[i - 1]!.high)
    highMax[i] = prevHigh

    const cur = data[i]!
    const range = Math.max(cur.high - cur.low, 0.01)
    const closePos = (cur.close - cur.low) / range
    const body = Math.abs(cur.close - cur.open)
    const upperShadow = cur.high - Math.max(cur.close, cur.open)

    if (cur.close > prevHigh && cur.volume > volume20 * 1.35 && closePos > 0.65) {
      markers.push({ time: cur.date as Time, position: 'belowBar', shape: 'arrowUp', color: '#ef4444', text: 'SOS', size: 1.2 })
    } else if (cur.volume < volume20 * 0.55 && closePos > 0.55) {
      markers.push({ time: cur.date as Time, position: 'belowBar', shape: 'circle', color: '#2563eb', text: '测试' })
    }

    if (upperShadow > Math.max(body * 1.8, range * 0.35) && closePos < 0.45 && cur.volume > volume20 * 1.15) {
      markers.push({ time: cur.date as Time, position: 'aboveBar', shape: 'arrowDown', color: '#10b981', text: '供应' })
    }
  }
  return markers.slice(-12)
}

function buildStructureSnapshot(
  data: KlineRow[],
  trOverride?: { support: number; resistance: number },
  stageOverride?: string,
): StructureSnapshot | null {
  if (data.length === 0) return null
  const latest = data[data.length - 1]!
  const previous = data[data.length - 2]
  const ma20 = data.length >= 20 ? avg(data.slice(-20).map((d) => d.close)) : latest.close
  const ma50 = data.length >= 50 ? avg(data.slice(-50).map((d) => d.close)) : ma20
  const levels = trOverride ?? buildPriceLevels(data)
  const volumeBase = data.length >= 21 ? avg(data.slice(-21, -1).map((d) => d.volume)) : avg(data.map((d) => d.volume))
  const changePct = previous ? (latest.close / previous.close - 1) * 100 : 0
  const volumeRatio = volumeBase > 0 ? latest.volume / volumeBase : 0

  if (stageOverride) {
    const tone = stageOverride === 'Markup' ? 'strong' : stageOverride === '回踩/弱势' ? 'weak' : 'watch'
    return { changePct, latestClose: latest.close, ma20, ma50, volumeRatio, ...levels, phase: stageOverride, tone }
  }

  const upperBand = levels.support + (levels.resistance - levels.support) * 0.72
  const lowerBand = levels.support + (levels.resistance - levels.support) * 0.28
  if (latest.close > ma20 && ma20 >= ma50 && latest.close >= upperBand) {
    return { changePct, latestClose: latest.close, ma20, ma50, volumeRatio, ...levels, phase: '右侧走强', tone: 'strong' }
  }
  if (latest.close < ma20 || latest.close <= lowerBand) {
    return { changePct, latestClose: latest.close, ma20, ma50, volumeRatio, ...levels, phase: '回踩/弱势', tone: 'weak' }
  }
  return { changePct, latestClose: latest.close, ma20, ma50, volumeRatio, ...levels, phase: '区间观察', tone: 'watch' }
}

function buildPriceLevels(data: KlineRow[]) {
  const recent = data.slice(-60)
  return {
    support: Math.min(...recent.map((d) => d.low)),
    resistance: Math.max(...recent.map((d) => d.high)),
  }
}

function formatPrice(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : '--'
}
