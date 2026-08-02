import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { CheckSquare, Loader2, Swords, XSquare } from 'lucide-react'
import { useAuthStore } from '@/stores/auth'
import { usePreferences } from '@/lib/preferences'
import { loadLLMConfigCandidates } from '@/lib/chat-agent'
import { streamLLMResponseWithFallback } from '@/lib/llm-stream'
import { clearStreamFlush, scheduleStreamFlush } from '@/lib/stream-render'
import { MarkdownContent } from '@/components/markdown'
import { KlineChart } from '@/components/kline-chart'
import { MultiStockChart, type ComparisonSeries } from '@/components/multi-stock-chart'
import { UpgradeNotice } from '@/components/upgrade-notice'
import { AIDisclaimer } from '@/components/ai-disclaimer'
import { TICKFLOW_PURCHASE, fetchValueSnapshotWithFetch, isSupportedKlineCode } from '@wyckoff/shared'
import type { KlineRow, ValueSnapshot } from '@wyckoff/shared'
import { formatSignedPercent } from '@/lib/format'
import { fetchKlineViaTickFlow, getUserDataKeys } from '@/lib/kline'
import { avg } from '@/lib/math'
import { saveAnalysisHistory } from '@/lib/local-history'
import { resolveStockQuery } from '@/lib/market-search'
import { sourceLabel, VALUE_RULESET_VERSION, valueTraceMeta, type ValueScore, type ValueTone } from '@wyckoff/shared'
import { buildValueDigest, buildValueScore, formatValuePercent, metricToneClass, numberTone, reverseNumberTone, signalClass, sortByValueRisk, valueDataQualityText, valueDataQualityTitle, valueScoreClass, valueUnavailableText, type ValueView } from '@/lib/value-analysis'

interface BattleTarget {
  code: string
  name: string
}

interface BattleStock extends BattleTarget {
  data: KlineRow[]
  stats: StrengthStats
  valueSnapshot: ValueSnapshot
}

interface StrengthStats {
  latestClose: number
  ret20: number
  ret60: number
  ret120: number
  drawdown60: number
  volumeRatio: number
  score: number
}

type ChartMode = 'overlay' | 'separate'

interface BattleHistoryPayload {
  input: string
  stocks: BattleStock[]
  selectedCodes: string[]
  mode: ChartMode
  overlayLimit: number
  report: string
  benchmark: KlineRow[]
  meta?: {
    inputSnapshotHash?: string
    promptVersion?: string
    model?: string
    generatedAt?: string
    valueSource?: string
    reportDate?: string
    valueRulesetVersion?: string
    valueDataQuality?: string
    valueRuleCodes?: string[]
    klineRows?: number
  }
}

const DEFAULT_INPUT = '中国平安\n贵州茅台\nAAPL\nNVDA\n腾讯'

export function StockBattlePage() {
  const user = useAuthStore((s) => s.user)
  const [input, setInput] = useState(DEFAULT_INPUT)
  const battle = useBattleRunner()
  const selectedSeries = useSelectedSeries(battle.stocks, battle.selectedCodes)
  useBattleHistory(user?.id, input, battle.stocks, battle.selectedCodes, battle.mode, battle.overlayLimit, battle.report, battle.benchmark, battle.model)

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-6">
      <BattleHeader />
      <BattleInput input={input} loading={battle.loading} onChange={setInput} onSubmit={() => battle.run(input)} />
      {battle.error && <UpgradeNotice message={battle.error} />}
      {battle.stocks.length > 0 && (
        <>
          <BattleControls battle={battle} />
          <BattleCharts mode={battle.mode} limit={battle.overlayLimit} stocks={selectedSeries} benchmark={battle.benchmark} />
          <ValueBattlePanel stocks={battle.stocks} />
          <StrengthTable stocks={battle.stocks} />
          <ReportPanel report={battle.report} loading={battle.loading} />
        </>
      )}
    </div>
  )
}

function useBattleRunner() {
  const user = useAuthStore((s) => s.user)
  const { t } = usePreferences()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [stocks, setStocks] = useState<BattleStock[]>([])
  const [selectedCodes, setSelectedCodes] = useState<string[]>([])
  const [mode, setMode] = useState<ChartMode>('overlay')
  const [overlayLimit, setOverlayLimit] = useState(6)
  const [report, setReport] = useState('')
  const [model, setModel] = useState('unknown')
  const abortRef = useRef<AbortController | null>(null)
  const streamBuf = useRef('')
  const flushTimer = useRef<ReturnType<typeof setTimeout> | 0>(0)
  const [benchmark, setBenchmark] = useState<KlineRow[]>([])

  async function run(input: string) {
    if (!user) return
    abortRef.current?.abort(); clearStreamFlush(flushTimer)
    const abort = new AbortController()
    abortRef.current = abort
    streamBuf.current = ''
    setLoading(true); setError(''); setStocks([]); setBenchmark([]); setSelectedCodes([]); setReport(''); setModel('unknown')
    try {
      const [configs, keys, targets] = await Promise.all([loadLLMConfigCandidates(user.id), getUserDataKeys(user.id), resolveTargets(input)])
      if (configs.length === 0) throw new Error(t('battle.missingModel'))
      setModel(configs[0]?.model || 'unknown')
      if (!keys.tickflow) throw new Error(upgradeMessage())
      const [fetched, bench] = await Promise.all([
        fetchBattleStocks(targets, keys),
        fetchKlineViaTickFlow('399300', keys.tickflow).catch(() => [] as KlineRow[]),
      ])
      if (abort.signal.aborted) return
      setStocks(fetched)
      setBenchmark(bench)
      setSelectedCodes(fetched.slice(0, Math.min(6, fetched.length)).map((item) => item.code))
      const onDelta = (chunk: string) => { streamBuf.current += chunk; scheduleStreamFlush(streamBuf, flushTimer, setReport) }
      const finalReport = await callBattleLLM(configs, fetched, abort.signal, onDelta, (nextModel) => setModel(nextModel))
      clearStreamFlush(flushTimer)
      if (abort.signal.aborted) return
      setReport(finalReport)
    } catch (err) {
      if (abort.signal.aborted) return
      setError(normalizeBattleError(err))
    } finally {
      clearStreamFlush(flushTimer)
      setLoading(false)
    }
  }

  return { loading, error, stocks, selectedCodes, mode, overlayLimit, report, benchmark, run, setSelectedCodes, setMode, setOverlayLimit, model }
}

function useBattleHistory(
  userId: string | undefined,
  input: string,
  stocks: BattleStock[],
  selectedCodes: string[],
  mode: ChartMode,
  overlayLimit: number,
  report: string,
  benchmark: KlineRow[],
  model: string,
) {
  const savedKey = useRef('')

  useEffect(() => {
    if (!userId || !report || stocks.length === 0) return
    const payload = buildBattleHistoryPayload(input, stocks, selectedCodes, mode, overlayLimit, report, benchmark, model)
    const key = battleHistoryKey(payload)
    if (savedKey.current === key) return
    savedKey.current = key
    void saveAnalysisHistory({
      kind: 'stock-battle',
      userId,
      title: payload.stocks.map((stock) => stock.name || stock.code).slice(0, 3).join(' / '),
      subtitle: `${payload.stocks.length} stocks`,
      symbols: payload.stocks.map((stock) => stock.code),
      payload,
    }).catch(() => undefined)
  }, [benchmark, input, mode, overlayLimit, report, selectedCodes, stocks, userId, model])
}

function buildBattleHistoryPayload(
  input: string,
  stocks: BattleStock[],
  selectedCodes: string[],
  mode: ChartMode,
  overlayLimit: number,
  report: string,
  benchmark: KlineRow[],
  model: string,
): BattleHistoryPayload {
  const rawText = `${VALUE_RULESET_VERSION}:${stocks.map(s => `${s.code}:${s.data.length}:${s.valueSnapshot.source}:${s.valueSnapshot.metrics ? JSON.stringify(s.valueSnapshot.metrics) : 'none'}`).join('|')}`
  let hash = 2166136261
  for (let i = 0; i < rawText.length; i++) {
    hash = Math.imul(hash ^ rawText.charCodeAt(i), 16777619)
  }
  const inputSnapshotHash = (hash >>> 0).toString(16)
  const valueTraces = stocks.map(stock => valueTraceMeta(stock.valueSnapshot))

  const meta = {
    inputSnapshotHash,
    promptVersion: 'wyckoff-prompt-v2.1',
    model,
    generatedAt: new Date().toISOString(),
    valueSource: stocks.map(s => sourceLabel(s.valueSnapshot)).filter(Boolean).join(','),
    reportDate: stocks.map(s => s.valueSnapshot.metrics?.period_end || 'unknown').filter(Boolean).join(','),
    valueRulesetVersion: VALUE_RULESET_VERSION,
    valueDataQuality: valueTraces.map(trace => trace.dataQuality).join(','),
    valueRuleCodes: [...new Set(valueTraces.flatMap(trace => trace.ruleCodes))],
    klineRows: stocks.reduce((acc, s) => acc + s.data.length, 0),
  }

  return {
    input,
    stocks,
    selectedCodes,
    mode,
    overlayLimit,
    report,
    benchmark,
    meta,
  }
}

function battleHistoryKey(payload: BattleHistoryPayload): string {
  return `${payload.stocks.map((stock) => stock.code).join(',')}:${payload.report.length}`
}

function BattleHeader() {
  const { t } = usePreferences()
  return (
    <header className="border-b border-border pb-5">
      <h1 className="flex items-center gap-2 text-xl font-semibold"><Swords size={21} />{t('battle.title')}</h1>
      <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{t('battle.subtitle')}</p>
    </header>
  )
}

function BattleInput({ input, loading, onChange, onSubmit }: { input: string; loading: boolean; onChange: (value: string) => void; onSubmit: () => void }) {
  const { t } = usePreferences()
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    onSubmit()
  }
  return (
    <form onSubmit={submit} className="rounded-lg border border-border p-4">
      <label className="text-sm font-medium">{t('battle.inputLabel')}</label>
      <textarea value={input} onChange={(e) => onChange(e.target.value)} className="mt-2 min-h-36 w-full rounded-lg border border-border bg-background p-3 text-sm outline-none focus:ring-2 focus:ring-ring/20" />
      <div className="mt-3 flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">{t('battle.inputHint')}</p>
        <button disabled={loading || !input.trim()} className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
          {loading ? <Loader2 size={16} className="animate-spin" /> : <Swords size={16} />}
          {loading ? t('battle.running') : t('battle.start')}
        </button>
      </div>
    </form>
  )
}

function BattleControls({ battle }: { battle: ReturnType<typeof useBattleRunner> }) {
  const { t } = usePreferences()
  return (
    <section className="rounded-lg border border-border p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <ModeSwitch mode={battle.mode} setMode={battle.setMode} />
        <OverlayLimit value={battle.overlayLimit} onChange={battle.setOverlayLimit} />
      </div>
      <SelectionGrid stocks={battle.stocks} selected={battle.selectedCodes} setSelected={battle.setSelectedCodes} />
      <div className="mt-3 flex gap-2">
        <SmallButton icon={<CheckSquare size={14} />} label={t('battle.selectAll')} onClick={() => battle.setSelectedCodes(battle.stocks.map((item) => item.code))} />
        <SmallButton icon={<XSquare size={14} />} label={t('battle.clearAll')} onClick={() => battle.setSelectedCodes([])} />
      </div>
    </section>
  )
}

function ModeSwitch({ mode, setMode }: { mode: ChartMode; setMode: (mode: ChartMode) => void }) {
  const { t } = usePreferences()
  return (
    <div className="inline-flex rounded-lg border border-border p-1 text-sm">
      <button type="button" onClick={() => setMode('overlay')} className={`rounded-md px-3 py-1.5 ${mode === 'overlay' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}>{t('battle.overlay')}</button>
      <button type="button" onClick={() => setMode('separate')} className={`rounded-md px-3 py-1.5 ${mode === 'separate' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}>{t('battle.separate')}</button>
    </div>
  )
}

function OverlayLimit({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  const { t } = usePreferences()
  return (
    <label className="flex items-center gap-2 text-sm text-muted-foreground">
      {t('battle.overlayLimit')}
      <input type="number" min={1} value={value} onChange={(e) => onChange(Math.max(1, Number(e.target.value) || 1))} className="w-20 rounded-lg border border-border bg-background px-2 py-1.5 text-foreground outline-none" />
    </label>
  )
}

function SelectionGrid({ stocks, selected, setSelected }: { stocks: BattleStock[]; selected: string[]; setSelected: (codes: string[]) => void }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {stocks.map((stock) => (
        <label key={stock.code} className="flex cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-muted/40">
          <input type="checkbox" checked={selected.includes(stock.code)} onChange={() => toggleSelected(stock.code, selected, setSelected)} />
          <span className="min-w-0 truncate">{stock.code} {stock.name}</span>
        </label>
      ))}
    </div>
  )
}

function BattleCharts({ mode, limit, stocks, benchmark }: { mode: ChartMode; limit: number; stocks: BattleStock[]; benchmark: KlineRow[] }) {
  if (stocks.length === 0) return null
  const benchSeries: ComparisonSeries | null = benchmark.length > 0 ? { code: '399300', name: '沪深300', data: benchmark } : null
  if (mode === 'overlay') {
    const series = stocks.slice(0, limit).map(toComparisonSeries)
    if (benchSeries) series.push(benchSeries)
    return <MultiStockChart series={series} />
  }
  return (
    <section className="grid gap-4 xl:grid-cols-2">
      {stocks.map((stock) => <SingleStockPanel key={stock.code} stock={stock} />)}
    </section>
  )
}

function SingleStockPanel({ stock }: { stock: BattleStock }) {
  const { t } = usePreferences()
  const value = buildValueScore(stock.valueSnapshot.metrics, t)
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="min-w-0 truncate text-sm font-semibold">{stock.code} {stock.name}</h2>
        <ValueBadge value={value} />
      </div>
      <KlineChart data={stock.data} height={300} />
    </div>
  )
}

function ValueBattlePanel({ stocks }: { stocks: BattleStock[] }) {
  const { t } = usePreferences()
  const [view, setView] = useState<ValueView>('quality')
  const rows = useMemo(() => sortByValueRisk(stocks, s => s.valueSnapshot.metrics), [stocks])
  return (
    <section className="rounded-lg border border-border p-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{t('battle.valueTitle')}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{t('battle.valueSubtitle')}</p>
        </div>
        <div className="inline-flex rounded-lg border border-border bg-muted/40 p-1" role="tablist" aria-label={t('battle.valueTitle')}>
          {(['quality', 'risk'] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setView(mode)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition ${view === mode ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
              role="tab"
              aria-selected={view === mode}
            >
              {mode === 'quality' ? t('analysis.valueQuality') : t('analysis.valueRisk')}
            </button>
          ))}
        </div>
      </div>
      <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
        {rows.map((stock) => <ValueBattleCard key={stock.code} stock={stock} view={view} />)}
      </div>
    </section>
  )
}

function ValueBattleCard({ stock, view }: { stock: BattleStock; view: ValueView }) {
  const { t } = usePreferences()
  const metrics = stock.valueSnapshot.metrics
  const value = buildValueScore(metrics, t)
  if (!metrics) {
    return (
      <div className="rounded-lg border border-border p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{stock.code} {stock.name}</h3>
            <p className="mt-1 text-xs text-muted-foreground">{valueUnavailableText(stock.valueSnapshot.reason, t)}</p>
          </div>
          <ValueBadge value={value} />
        </div>
      </div>
    )
  }
  const signals = view === 'quality' ? value.strengths : value.risks
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold">{stock.code} {stock.name}</h3>
          <p title={valueDataQualityTitle(stock.valueSnapshot, t)} className="mt-1 text-xs text-muted-foreground">{sourceLabel(stock.valueSnapshot)}{metrics.period_end ? ` · ${metrics.period_end}` : ''} · {valueDataQualityText(stock.valueSnapshot, t)}</p>
        </div>
        <ValueBadge value={value} />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
        <MetricCell label={t('analysis.valueRoe')} value={formatValuePercent(metrics.roe)} tone={numberTone(metrics.roe, 10, 0)} />
        <MetricCell label={t('analysis.valueProfitYoy')} value={formatValuePercent(metrics.net_income_yoy)} tone={numberTone(metrics.net_income_yoy, 0, -10)} />
        <MetricCell label={t('analysis.valueGrossMargin')} value={formatValuePercent(metrics.gross_margin)} tone={numberTone(metrics.gross_margin, 30, 15)} />
        <MetricCell label={t('analysis.valueDebtRatio')} value={formatValuePercent(metrics.debt_to_asset_ratio)} tone={reverseNumberTone(metrics.debt_to_asset_ratio, 55, 70)} />
      </div>
      <div className="mt-3 space-y-2">
        {signals.length > 0 ? signals.slice(0, 3).map((signal) => (
          <div key={signal.label} className={`rounded-md border px-3 py-2 text-xs ${signalClass(signal.tone)}`}>{signal.label}</div>
        )) : (
          <div className="rounded-md border border-border px-3 py-2 text-xs text-muted-foreground">{t('analysis.valueNoSignals')}</div>
        )}
      </div>
    </div>
  )
}

function MetricCell({ label, value, tone }: { label: string; value: string; tone: ValueTone }) {
  return (
    <div className="min-w-0">
      <div className="truncate text-xs text-muted-foreground">{label}</div>
      <div className={`mt-0.5 font-semibold ${metricToneClass(tone)}`}>{value}</div>
    </div>
  )
}

function ValueBadge({ value }: { value: ValueScore }) {
  return <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${valueScoreClass(value.tone, value.severe)}`}>{value.label}</span>
}

function StrengthTable({ stocks }: { stocks: BattleStock[] }) {
  const { t } = usePreferences()
  const rows = [...stocks].sort((a, b) => b.stats.score - a.stats.score)
  return (
    <section className="overflow-hidden rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead className="bg-muted/40"><tr>{['#', t('common.code'), t('battle.score'), t('battle.valueColumn'), '20D', '60D', '120D', t('battle.drawdown')].map((h) => <th key={h} scope="col" className="px-3 py-2 text-left font-medium">{h}</th>)}</tr></thead>
        <tbody>{rows.map((stock, index) => <StrengthRow key={stock.code} stock={stock} rank={index + 1} />)}</tbody>
      </table>
    </section>
  )
}

function StrengthRow({ stock, rank }: { stock: BattleStock; rank: number }) {
  const { t } = usePreferences()
  return (
    <tr className="border-t border-border">
      <td className="px-3 py-2">{rank}</td>
      <td className="px-3 py-2 font-mono">{stock.code} <span className="font-sans text-muted-foreground">{stock.name}</span></td>
      <td className="px-3 py-2 font-medium">{stock.stats.score.toFixed(1)}</td>
      <td className="px-3 py-2"><ValueBadge value={buildValueScore(stock.valueSnapshot.metrics, t)} /></td>
      <td className="px-3 py-2">{formatSignedPercent(stock.stats.ret20)}</td>
      <td className="px-3 py-2">{formatSignedPercent(stock.stats.ret60)}</td>
      <td className="px-3 py-2">{formatSignedPercent(stock.stats.ret120)}</td>
      <td className="px-3 py-2">{formatSignedPercent(stock.stats.drawdown60)}</td>
    </tr>
  )
}

function ReportPanel({ report, loading }: { report: string; loading: boolean }) {
  const { t } = usePreferences()
  if (!report && !loading) return null
  return (
    <section className="rounded-lg border border-border p-5">
      <h2 className="mb-4 text-base font-semibold">{t('battle.report')}</h2>
      {report ? (
        <>
          <AIDisclaimer />
          <article className="mt-4 prose prose-sm max-w-none text-foreground"><MarkdownContent content={report} streaming={loading} /></article>
        </>
      ) : (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={16} className="animate-spin" />
          <span>正在生成对抗结论...</span>
        </div>
      )}
    </section>
  )
}

function SmallButton({ icon, label, onClick }: { icon: ReactNode; label: string; onClick: () => void }) {
  return <button type="button" onClick={onClick} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted">{icon}{label}</button>
}

function useSelectedSeries(stocks: BattleStock[], selectedCodes: string[]) {
  return useMemo(() => stocks.filter((item) => selectedCodes.includes(item.code)), [stocks, selectedCodes])
}

function toggleSelected(code: string, selected: string[], setSelected: (codes: string[]) => void) {
  setSelected(selected.includes(code) ? selected.filter((item) => item !== code) : [...selected, code])
}

function parseInput(input: string): string[] {
  return Array.from(new Set(input.split(/[\s,，;；、]+/).map((item) => item.trim()).filter(Boolean)))
}

async function resolveTargets(input: string): Promise<BattleTarget[]> {
  const tokens = parseInput(input)
  const rows = await Promise.all(tokens.map(resolveToken))
  const byCode = new Map<string, BattleTarget>()
  for (const row of rows) if (row) byCode.set(row.code, row)
  if (byCode.size === 0) throw new Error('请输入至少一只有效股票代码或名称')
  return [...byCode.values()]
}

async function resolveToken(token: string): Promise<BattleTarget | null> {
  const resolved = await resolveStockQuery(token)
  const code = resolved?.analysisCode || token.toUpperCase()
  if (!isSupportedKlineCode(code)) return null
  return { code, name: resolved?.name || code }
}

async function fetchBattleStocks(targets: BattleTarget[], keys: { tickflow: string | null; tushare: string | null }): Promise<BattleStock[]> {
  const errors: string[] = []
  const stocks: BattleStock[] = []
  await Promise.all(
    targets.map(async (target) => {
      try {
        const result = await fetchOneBattleStock(target, keys)
        if (result) stocks.push(result)
        else errors.push(`${target.code}: 无数据`)
      } catch (err) {
        errors.push(`${target.code}: ${err instanceof Error ? err.message : '失败'}`)
      }
    }),
  )
  if (errors.length > 0) throw new Error(`K 线获取失败: ${errors.join(', ')}`)
  if (stocks.length === 0) throw new Error('没有获取到有效 K 线数据')
  return stocks
}

async function fetchOneBattleStock(target: BattleTarget, keys: { tickflow: string | null; tushare: string | null }): Promise<BattleStock | null> {
  if (!keys.tickflow) return null
  const [data, valueSnapshot] = await Promise.all([
    fetchKlineViaTickFlow(target.code, keys.tickflow),
    fetchValueSnapshotWithFetch(globalThis.fetch, target.code, keys).catch((): ValueSnapshot => ({ symbol: target.code, source: 'none', metrics: null, reason: 'not-found' })),
  ])
  if (data.length === 0) return null
  return { ...target, data, stats: computeStrengthStats(data), valueSnapshot }
}

function computeStrengthStats(data: KlineRow[]): StrengthStats {
  const latest = data[data.length - 1]!
  const ret20 = periodReturn(data, 20), ret60 = periodReturn(data, 60), ret120 = periodReturn(data, 120)
  const recent60 = data.slice(-60), high60 = Math.max(...recent60.map((row) => row.high))
  const volumeBase = data.length > 21 ? avg(data.slice(-21, -1).map((row) => row.volume)) : avg(data.map((row) => row.volume))
  const volumeRatio = volumeBase > 0 ? latest.volume / volumeBase : 0
  const drawdown60 = high60 > 0 ? (latest.close / high60 - 1) * 100 : 0
  const score = ret20 * 0.35 + ret60 * 0.35 + ret120 * 0.2 + drawdown60 * 0.1 + Math.min(volumeRatio, 3)
  return { latestClose: latest.close, ret20, ret60, ret120, drawdown60, volumeRatio, score }
}

function periodReturn(data: KlineRow[], days: number): number {
  const latest = data[data.length - 1]?.close || 0
  const base = data[Math.max(0, data.length - days - 1)]?.close || latest
  return base > 0 ? (latest / base - 1) * 100 : 0
}

async function callBattleLLM(configs: Parameters<typeof streamLLMResponseWithFallback>[0], stocks: BattleStock[], signal?: AbortSignal, onDelta?: (chunk: string) => void, onModel?: (model: string) => void): Promise<string> {
  const result = await streamLLMResponseWithFallback(configs, buildBattleMessages(stocks), { temperature: 0.45, maxTokens: 3500, signal, onDelta, onStatus: (status) => onModel?.(status.nextModel || status.model) })
  if (!result) throw new Error('模型未返回结果，请重试')
  return result
}

export const BATTLE_SYSTEM_PROMPT = `你是威科夫强弱对抗分析师。主框架是量价相对强弱、趋势延续性和回撤位置；若给出价值面摘要，只把它作为质量、风险和置信度校准，不用基本面替代 K 线事实。

【核心质量要求】
- 必须在报告中说明数据来源、给出明确的置信度理由与风控建议，并提供策略失效判定条件。
- 绝对禁止在分析结论中使用“必然”、“保证”、“无风险”、“稳赚”、“稳赢”、“包赚”等夸大或确定性的承诺词语。

输出结构：
1. 强弱排序及核心胜出原因。
2. 各标的落后风险及价值面校准。
3. 各标的适合观察的触发价位、防守生死线/策略失效条件。`

function buildBattleMessages(stocks: BattleStock[]) {
  return [
    { role: 'system' as const, content: BATTLE_SYSTEM_PROMPT },
    { role: 'user' as const, content: `请比较这些股票的强弱，并给出结论。\n\n${stocks.map(buildStockDigest).join('\n\n---\n\n')}` },
  ]
}

function buildStockDigest(stock: BattleStock): string {
  const rows = stock.data.slice(-60).map((row) => [row.date, row.open, row.high, row.low, row.close, Math.round(row.volume)].join(','))
  return [`## ${stock.code} ${stock.name}`, `score=${stock.stats.score.toFixed(2)} ret20=${stock.stats.ret20.toFixed(2)} ret60=${stock.stats.ret60.toFixed(2)} ret120=${stock.stats.ret120.toFixed(2)} drawdown60=${stock.stats.drawdown60.toFixed(2)} volumeRatio=${stock.stats.volumeRatio.toFixed(2)}`, buildValueDigest(stock.valueSnapshot), '```csv', 'date,open,high,low,close,volume', ...rows, '```'].join('\n')
}

function normalizeBattleError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err)
  return message.includes(TICKFLOW_PURCHASE) ? upgradeMessage() : message
}

function upgradeMessage(): string {
  return `触发数据源并发请求限制，请升级数据源：${TICKFLOW_PURCHASE}`
}

function toComparisonSeries(stock: BattleStock): ComparisonSeries {
  return { code: stock.code, name: stock.name, data: stock.data }
}
