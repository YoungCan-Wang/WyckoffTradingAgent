import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { Loader2, Play } from 'lucide-react'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'
import { loadLLMConfig, loadLLMConfigCandidates } from '@/lib/chat-agent'
import { streamLLMResponseWithFallback, type LLMStreamStatus } from '@/lib/llm-stream'
import { clearStreamFlush, scheduleStreamFlush } from '@/lib/stream-render'
import { MarkdownContent } from '@/components/markdown'
import { KlineChart } from '@/components/kline-chart'
import { NewsEventCards } from '@/components/news-event-cards'
import { useNewsChartEvents } from '@/lib/news-chart-events'
import { StockSearchBox, matchesSelectedQuery, useStockSearch, type StockSearchController } from '@/components/stock-search-box'
import { usePreferences } from '@/lib/preferences'
import { AIDisclaimer } from '@/components/ai-disclaimer'
import { detectWyckoffAnnotations } from '@/lib/wyckoff-detect'
import { TICKFLOW_PURCHASE, buildStockAnalysisContextPack, fetchValueSnapshotWithFetch, formatAnalysisContextPack, isCnSymbol, isSupportedKlineCode } from '@wyckoff/shared'
import type { AnalysisContextPack, KlineDataQuality, KlineRow, ValueSnapshot } from '@wyckoff/shared'
import { fetchKlineWithQuality, getUserDataKeys } from '@/lib/kline'
import { avg } from '@/lib/math'
import { resolveStockQuery, type StockSearchResult } from '@/lib/market-search'
import { buildValuePrompt, sourceLabel, valueTraceMeta } from '@wyckoff/shared'
import { buildValueScore, calculateInputSnapshotHash, formatValuePercent, metricToneClass, numberTone, reverseNumberTone, signalClass, valueDataQualityText, valueDataQualityTitle, valueScoreClass, valueUnavailableText, type ValueView } from '@/lib/value-analysis'
import { saveAnalysisHistory } from '@/lib/local-history'

interface AnalysisResult {
  report: string
  symbol: string
  name: string
  klineData: KlineRow[]
  dataQuality: KlineDataQuality
  valueSnapshot: ValueSnapshot
  contextPack: AnalysisContextPack
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

export function AnalysisPage() {
  const user = useAuthStore((s) => s.user)
  const { t } = usePreferences()
  const search = useAnalysisStockSearch()
  const prerequisites = usePrerequisites(user?.id)
  const runner = useAnalysisRunner(search, prerequisites.setHasModelConfig)
  useAnalysisHistory(user?.id, runner.result)
  const navigate = useNavigate()
  const disabled = runner.loading || !search.symbol.trim() || prerequisites.checkingConfig || !prerequisites.hasModelConfig || !prerequisites.hasDataSource

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden px-6 py-5">
      <div className="shrink-0 border-b border-border/70 pb-4">
        <h1 className="mb-4 text-xl font-semibold">{t('analysis.title')}</h1>
        <MissingConfigBanner prerequisites={prerequisites} />
        <SearchForm search={search} loading={runner.loading} disabled={disabled} onAnalyze={runner.handleAnalyze} onClearError={() => runner.setError('')} />
        {runner.error && <div className="mt-3 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-200">{runner.error}</div>}
      </div>
      <AnalysisContent
        runner={runner}
        onAskAboutRange={(start, end) => navigate('/chat', {
          state: { initialPrompt: `请分析 ${runner.result?.symbol || search.symbol} 从 ${start} 到 ${end} 这段 K 线的威科夫结构、量价变化和关键风险。先调用工具读取数据，再给出结论。` },
        })}
      />
    </div>
  )
}

function useAnalysisStockSearch(): StockSearchController {
  const search = useStockSearch('analysis')
  useUrlSymbol(search.setSymbol)
  return search
}

function useAnalysisHistory(userId: string | undefined, result: AnalysisResult | null) {
  const savedKey = useRef('')

  useEffect(() => {
    if (!userId || !result?.report) return
    const key = analysisHistoryKey(result)
    if (savedKey.current === key) return
    savedKey.current = key
    void saveAnalysisHistory({
      kind: 'single-analysis',
      userId,
      title: `${result.symbol} ${result.name}`,
      subtitle: `${result.klineData.length} rows · ${sourceLabel(result.valueSnapshot)}`,
      symbols: [result.symbol],
      payload: result,
    }).catch(() => undefined)
  }, [result, userId])
}

function useUrlSymbol(setSymbol: Dispatch<SetStateAction<string>>) {
  const [searchParams] = useSearchParams()
  useEffect(() => {
    const code = searchParams.get('code')?.trim().toUpperCase()
    if (code && isSupportedKlineCode(code)) setSymbol(code)
  }, [searchParams, setSymbol])
}

interface Prerequisites {
  checkingConfig: boolean
  hasModelConfig: boolean
  hasDataSource: boolean
  setHasModelConfig: Dispatch<SetStateAction<boolean>>
}

function usePrerequisites(userId: string | undefined): Prerequisites {
  const [checkingConfig, setCheckingConfig] = useState(true)
  const [hasModelConfig, setHasModelConfig] = useState(false)
  const [hasDataKeys, setHasDataKeys] = useState(false)

  useEffect(() => {
    if (!userId) return
    setCheckingConfig(true)
    void Promise.all([loadLLMConfig(userId), getUserDataKeys(userId)])
      .then(([config, dataKeys]) => {
        setHasModelConfig(Boolean(config?.api_key && config.model))
        setHasDataKeys(Boolean(dataKeys.tickflow || dataKeys.tushare))
      })
      .finally(() => setCheckingConfig(false))
  }, [userId])

  return {
    checkingConfig,
    hasModelConfig,
    hasDataSource: hasDataKeys,
    setHasModelConfig,
  }
}

type AnalysisStep = 'resolve' | 'kline' | 'llm'

interface AnalysisRunnerState {
  loading: boolean
  result: AnalysisResult | null
  error: string
  step: AnalysisStep | null
  streamingReport: string
  modelStatus: LLMStreamStatus | null
  earlyKline: { data: KlineRow[]; symbol: string; name: string; dataQuality: KlineDataQuality; valueSnapshot: ValueSnapshot; contextPack: AnalysisContextPack } | null
  setError: Dispatch<SetStateAction<string>>
  handleAnalyze: () => void
}

function useAnalysisRunner(search: StockSearchController, setHasModelConfig: Dispatch<SetStateAction<boolean>>): AnalysisRunnerState {
  const user = useAuthStore((s) => s.user)
  const { t } = usePreferences()
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [error, setError] = useState('')
  const [step, setStep] = useState<AnalysisStep | null>(null)
  const [streamingReport, setStreamingReport] = useState('')
  const [modelStatus, setModelStatus] = useState<LLMStreamStatus | null>(null)
  const [earlyKline, setEarlyKline] = useState<{ data: KlineRow[]; symbol: string; name: string; dataQuality: KlineDataQuality; valueSnapshot: ValueSnapshot; contextPack: AnalysisContextPack } | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const streamBuf = useRef('')
  const flushTimer = useRef<ReturnType<typeof setTimeout> | 0>(0)

  async function handleAnalyze() {
    const userId = user?.id
    if (!userId) { setError(t('chat.requestFailed')); return }
    setStep('resolve')
    const resolved = await resolveAnalysisCode(search.symbol, search.selectedStock)
    if (!resolved) { setError(t('analysis.invalidStockCode')); setStep(null); return }
    const abort = startAnalysisRequest(abortRef, search, resolved, setError, setLoading, setResult, setStreamingReport, setEarlyKline, setModelStatus)
    try {
      const [configs, dataKeys] = await Promise.all([loadLLMConfigCandidates(userId), getUserDataKeys(userId)])
      const config = configs[0]
      setHasModelConfig(Boolean(config?.api_key && config?.model))
      if (!config?.api_key || !config.model) throw new Error(t('analysis.missingPrefix', { items: t('analysis.modelRequirement') }))
      setStep('kline')
      const [stockInfoResult, klineData, valueSnapshot] = await Promise.all([
        fetchStockName(resolved.code),
        fetchKlineWithQuality(resolved.code, dataKeys, userId),
        fetchValueSnapshotWithFetch(globalThis.fetch, resolved.code, dataKeys).catch((): ValueSnapshot => ({ symbol: resolved.code, source: 'none', metrics: null, reason: 'not-found' })),
      ])
      if (klineData.data.length === 0) throw new Error(t('analysis.noKlineData'))
      const name = resolved.stock?.name || stockInfoResult.data?.name || resolved.code
      const contextPack = buildStockAnalysisContextPack({ symbol: resolved.code, name, kline: klineData.data, dataQuality: klineData.quality, valueSnapshot })
      setEarlyKline({ data: klineData.data, symbol: resolved.code, name, dataQuality: klineData.quality, valueSnapshot, contextPack })
      setStep('llm'); streamBuf.current = ''
      const onDelta = (chunk: string) => { streamBuf.current += chunk; scheduleStreamFlush(streamBuf, flushTimer, setStreamingReport) }
      const report = await callLLM(configs, resolved.code, name, buildKlinePayload(klineData.data, klineData.quality, contextPack), valueSnapshot, abort.signal, onDelta, setModelStatus)
      clearStreamFlush(flushTimer)
      if (abort.signal.aborted) return
      setStreamingReport(report)
      const inputSnapshotHash = calculateInputSnapshotHash(resolved.code, klineData.data, valueSnapshot)
      const valueTrace = valueTraceMeta(valueSnapshot)
      const meta = {
        inputSnapshotHash,
        promptVersion: 'wyckoff-prompt-v2.1',
        model: config?.model || 'unknown',
        generatedAt: new Date().toISOString(),
        valueSource: valueSnapshot.source,
        reportDate: valueSnapshot.metrics?.period_end || valueSnapshot.metrics?.announce_date || 'unknown',
        valueRulesetVersion: valueTrace.rulesetVersion,
        valueDataQuality: valueTrace.dataQuality,
        valueRuleCodes: valueTrace.ruleCodes,
        klineRows: klineData.data.length,
      }
      setResult({ report, symbol: resolved.code, name, klineData: klineData.data, dataQuality: klineData.quality, valueSnapshot, contextPack, meta })
    } catch (err) {
      if (abort.signal.aborted) return
      setError(err instanceof Error ? err.message : t('analysis.failed'))
    } finally { clearStreamFlush(flushTimer); setLoading(false); setStep(null) }
  }

  return { loading, result, error, step, streamingReport, modelStatus, earlyKline, setError, handleAnalyze }
}

function startAnalysisRequest(
  abortRef: React.MutableRefObject<AbortController | null>,
  search: StockSearchController,
  resolved: NonNullable<Awaited<ReturnType<typeof resolveAnalysisCode>>>,
  setError: Dispatch<SetStateAction<string>>,
  setLoading: Dispatch<SetStateAction<boolean>>,
  setResult: Dispatch<SetStateAction<AnalysisResult | null>>,
  setStreamingReport: Dispatch<SetStateAction<string>>,
  setEarlyKline: Dispatch<SetStateAction<AnalysisRunnerState['earlyKline']>>,
  setModelStatus: Dispatch<SetStateAction<LLMStreamStatus | null>>,
): AbortController {
  abortRef.current?.abort()
  const abort = new AbortController()
  abortRef.current = abort
  setError(''); setLoading(true); setResult(null); setStreamingReport(''); setEarlyKline(null); setModelStatus(null)
  search.setSymbol(resolved.code); search.setSelectedStock(resolved.stock); search.setSearchOpen(false)
  return abort
}

function analysisHistoryKey(result: AnalysisResult): string {
  return `${result.symbol}:${result.klineData.length}:${result.report.length}`
}

function MissingConfigBanner({ prerequisites }: { prerequisites: Prerequisites }) {
  const navigate = useNavigate()
  const { t } = usePreferences()
  if (prerequisites.checkingConfig || (prerequisites.hasModelConfig && prerequisites.hasDataSource)) return null
  return (
    <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50/80 p-4 dark:border-amber-500/30 dark:bg-amber-500/10">
      <h2 className="mb-2 text-sm font-semibold text-amber-900 dark:text-amber-100">{t('analysis.missingTitle')}</h2>
      <ul className="mb-3 list-disc space-y-1 pl-5 text-sm text-amber-800 dark:text-amber-200">
        {!prerequisites.hasModelConfig && <li>{t('analysis.missingModel')}</li>}
        {!prerequisites.hasDataSource && <li>{t('analysis.missingDataSource')}</li>}
      </ul>
      <button onClick={() => navigate('/settings')} className="rounded-lg bg-amber-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-800">
        {t('analysis.goSettings')}
      </button>
    </div>
  )
}

function SearchForm({
  search,
  loading,
  disabled,
  onAnalyze,
  onClearError,
}: {
  search: StockSearchController
  loading: boolean
  disabled: boolean
  onAnalyze: () => void
  onClearError: () => void
}) {
  const { t } = usePreferences()
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[240px] flex-1 lg:max-w-3xl">
          <StockSearchBox
            search={search}
            onSubmit={onAnalyze}
            onClearError={onClearError}
            placeholder={t('analysis.searchPlaceholder')}
            listboxId="analysis-stock-search"
          />
        </div>
        <button onClick={onAnalyze} disabled={disabled} className="flex h-10 shrink-0 items-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground disabled:opacity-50">
          {loading ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
          {loading ? t('analysis.analyzing') : t('analysis.start')}
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        {t('analysis.marketHint')}
        <a href={TICKFLOW_PURCHASE} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">{t('common.tickflowLink')}</a>
      </p>
    </div>
  )
}

function AnalysisContent({ runner, onAskAboutRange }: { runner: AnalysisRunnerState; onAskAboutRange: (start: string, end: string) => void }) {
  const { result, loading, step, streamingReport, modelStatus, earlyKline } = runner
  if (!result && !loading) return <EmptyAnalysisState />

  const kline = result?.klineData ?? earlyKline?.data
  const symbol = result?.symbol ?? earlyKline?.symbol
  const name = result?.name ?? earlyKline?.name
  const report = result?.report ?? streamingReport
  const valueSnapshot = result?.valueSnapshot ?? earlyKline?.valueSnapshot
  const dataQuality = result?.dataQuality ?? earlyKline?.dataQuality
  const contextPack = result?.contextPack ?? earlyKline?.contextPack

  return (
    <div className="flex min-h-0 flex-1 flex-col pt-4">
      {step && <AnalysisProgressBar step={step} modelStatus={modelStatus} />}
      {symbol && name && (
        <div className="mb-3 flex shrink-0 items-center gap-2">
          <span className="rounded-full bg-primary/10 px-3 py-1 text-sm font-medium text-primary">{symbol} {name}</span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto pr-1">
        <div className="space-y-6">
          {kline && dataQuality && <KlineSection symbol={symbol} name={name} klineData={kline} dataQuality={dataQuality} onAskAboutRange={onAskAboutRange} />}
          {contextPack && <ContextPackSection pack={contextPack} />}
          {valueSnapshot && <ValueSection snapshot={valueSnapshot} />}
          {report && <ReportSection report={report} streaming={loading && !result} />}
        </div>
      </div>
    </div>
  )
}

function ContextPackSection({ pack }: { pack: AnalysisContextPack }) {
  return (
    <details className="rounded-lg border border-border bg-muted/20 px-4 py-3">
      <summary className="cursor-pointer text-sm font-medium">本次分析上下文包与证据</summary>
      <div className="mt-3 space-y-2 text-xs text-muted-foreground">
        <pre className="whitespace-pre-wrap rounded-md bg-background p-3 leading-5">{formatAnalysisContextPack(pack)}</pre>
      </div>
    </details>
  )
}

function KlineSection({ symbol, name, klineData, dataQuality, compact = false, onAskAboutRange }: { symbol?: string; name?: string; klineData: KlineRow[]; dataQuality: KlineDataQuality; compact?: boolean; onAskAboutRange: (start: string, end: string) => void }) {
  const { t } = usePreferences()
  const wyckoff = useMemo(() => detectWyckoffAnnotations(klineData), [klineData])
  const sessionDates = useMemo(() => klineData.map((row) => row.date), [klineData])
  const news = useNewsChartEvents(symbol, sessionDates, name)
  const newsMarkers = useMemo(() => news.events.map((event) => ({ date: event.date, sentiment: event.sentiment, label: event.kind })), [news.events])
  const [selectedRange, setSelectedRange] = useState<{ start: string; end: string } | null>(null)
  const handleBarClick = (date: string) => {
    setSelectedRange((current) => {
      if (!current || current.start !== current.end) return { start: date, end: date }
      return current.start <= date ? { start: current.start, end: date } : { start: date, end: current.start }
    })
  }
  const range = selectedRange && {
    start: selectedRange.start,
    end: selectedRange.end,
    label: selectedRange.start === selectedRange.end ? selectedRange.start : `${selectedRange.start} 至 ${selectedRange.end}`,
  }
  return (
    <section>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div><h2 className="text-base font-semibold">{t('analysis.chartTitle')}</h2><p className="mt-1 text-xs text-muted-foreground">{t('analysis.chartSubtitle')}</p></div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground">{klineData.length} {t('common.rows')}</span>
          <DataQualityBadge quality={dataQuality} />
        </div>
      </div>
      <KlineChart data={klineData} height={compact ? 320 : 430} wyckoffMarkers={wyckoff?.markers} newsMarkers={newsMarkers} tradingRange={wyckoff?.tradingRange ?? undefined} stage={wyckoff?.stage} showIndicators onBarClick={handleBarClick} />
      <NewsEventCards events={news.events} status={news.status} />
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
        <span>{range ? `已选 ${range.label}` : '点击一根 K 线开始选段，再点击另一根确定区间'}</span>
        <div className="flex items-center gap-2">
          {range && <button type="button" onClick={() => setSelectedRange(null)} className="rounded-md border border-border px-2 py-1 hover:bg-muted/60">清除</button>}
          {range && <button type="button" onClick={() => onAskAboutRange(range.start, range.end)} className="rounded-md bg-primary px-2.5 py-1 font-medium text-primary-foreground hover:opacity-90">询问这段走势</button>}
        </div>
      </div>
    </section>
  )
}

function DataQualityBadge({ quality }: { quality: KlineDataQuality }) {
  const source = quality.source === 'tickflow' ? 'TickFlow' : quality.source === 'tushare' ? 'Tushare' : quality.source === 'mixed' ? '多源' : '无来源'
  const coverage = quality.coverageStart && quality.coverageEnd ? `${quality.coverageStart} 至 ${quality.coverageEnd}` : '无覆盖区间'
  return (
    <span title={`来源：${source}；覆盖：${coverage}；最新交易日：${quality.latestTradingDate || '未知'}`} className={`rounded-full border px-2.5 py-1 text-xs ${quality.isComplete ? 'border-border text-muted-foreground' : 'border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200'}`}>
      {source} · {quality.latestTradingDate || '未知'}{quality.fallbackUsed ? ' · 已回退' : ''}
    </span>
  )
}

function ValueSection({ snapshot, compact = false }: { snapshot: ValueSnapshot; compact?: boolean }) {
  const { t } = usePreferences()
  const [view, setView] = useState<ValueView>('quality')
  const metrics = snapshot.metrics
  const signals = useMemo(() => metrics ? buildValueScore(metrics, t) : null, [metrics, t])

  if (!metrics) {
    return (
      <section className={`rounded-lg border border-border bg-background ${compact ? 'p-4' : 'p-5'}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">{t('analysis.valueTitle')}</h2>
            <p className="mt-1 text-xs text-muted-foreground">{t('analysis.valueSubtitle')}</p>
          </div>
          <span className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground">{t('analysis.valueNoSource')}</span>
        </div>
        <p className="mt-4 text-sm text-muted-foreground">{valueUnavailableText(snapshot.reason, t)}</p>
      </section>
    )
  }

  const shownSignals = view === 'quality' ? signals?.strengths ?? [] : signals?.risks ?? []
  const metricItems = [
    { label: t('analysis.valueRoe'), value: formatValuePercent(metrics.roe), tone: numberTone(metrics.roe, 10, 0) },
    { label: t('analysis.valueProfitYoy'), value: formatValuePercent(metrics.net_income_yoy), tone: numberTone(metrics.net_income_yoy, 0, -10) },
    { label: t('analysis.valueRevenueYoy'), value: formatValuePercent(metrics.revenue_yoy), tone: numberTone(metrics.revenue_yoy, 0, -10) },
    { label: t('analysis.valueGrossMargin'), value: formatValuePercent(metrics.gross_margin), tone: numberTone(metrics.gross_margin, 30, 15) },
    { label: t('analysis.valueDebtRatio'), value: formatValuePercent(metrics.debt_to_asset_ratio), tone: reverseNumberTone(metrics.debt_to_asset_ratio, 55, 70) },
    { label: t('analysis.valueCashRevenue'), value: formatValuePercent(metrics.operating_cash_to_revenue), tone: numberTone(metrics.operating_cash_to_revenue, 5, 0) },
  ]

  return (
    <section className={`rounded-lg border border-border bg-background ${compact ? 'p-4' : 'p-5'}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{t('analysis.valueTitle')}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{t('analysis.valueSubtitle')}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${valueScoreClass(signals?.tone ?? 'neutral', signals?.severe ?? false)}`}>{signals?.label}</span>
          <span className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground">{sourceLabel(snapshot)}</span>
          <span title={valueDataQualityTitle(snapshot, t)} className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground">{valueDataQualityText(snapshot, t)}</span>
        </div>
      </div>

      <div className="mt-4 grid gap-x-4 gap-y-3 border-y border-border/70 py-4 sm:grid-cols-2 lg:grid-cols-3">
        {metricItems.map((item) => (
          <div key={item.label} className="min-w-0">
            <div className="truncate text-xs text-muted-foreground">{item.label}</div>
            <div className={`mt-1 text-lg font-semibold ${metricToneClass(item.tone)}`}>{item.value}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex rounded-lg border border-border bg-muted/40 p-1" role="tablist" aria-label={t('analysis.valueTitle')}>
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
        {(metrics.period_end || metrics.announce_date) && <span className="text-xs text-muted-foreground">{t('analysis.valuePeriod')}: {metrics.period_end || metrics.announce_date}</span>}
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {shownSignals.length > 0 ? shownSignals.map((signal) => (
          <div key={signal.label} className={`rounded-md border px-3 py-2.5 text-sm ${signalClass(signal.tone)} flex flex-col gap-1`}>
            <div className="flex items-center justify-between gap-2 font-medium">
              <span>{signal.label}</span>
              {signal.code && <span className="text-[10px] uppercase tracking-wider font-mono opacity-60 bg-foreground/10 px-1.5 py-0.5 rounded shrink-0">{signal.code}</span>}
            </div>
            {signal.explanation && <div className="text-xs opacity-80 leading-normal mt-0.5">{signal.explanation}</div>}
          </div>
        )) : (
          <div className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground">{t('analysis.valueNoSignals')}</div>
        )}
      </div>
    </section>
  )
}

function ReportSection({ report, streaming }: { report: string; streaming: boolean }) {
  const { t } = usePreferences()
  return (
    <section className="min-w-0 rounded-lg border border-border bg-background">
      <div className="border-b border-border/70 px-5 py-4">
        <h2 className="mb-3 text-base font-semibold">{t('analysis.reportTitle')}</h2>
        <AIDisclaimer />
      </div>
      <article className="prose prose-base max-w-none px-6 py-5 text-foreground">
        <MarkdownContent content={report} streaming={streaming} />
      </article>
    </section>
  )
}

function AnalysisProgressBar({ step, modelStatus }: { step: AnalysisStep; modelStatus: LLMStreamStatus | null }) {
  const { t } = usePreferences()
  const stages: { key: AnalysisStep; label: string; pct: number }[] = [
    { key: 'resolve', label: t('analysis.progressResolve'), pct: 5 },
    { key: 'kline', label: t('analysis.progressKline'), pct: 30 },
    { key: 'llm', label: t('analysis.progressLLM'), pct: 60 },
  ]
  const current = stages.find((s) => s.key === step) ?? stages[0]!
  return (
    <div className="mb-4 rounded-lg border border-border bg-muted/10 px-4 py-2.5">
      <div className="mb-1.5 flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{modelStatus ? (modelStatus.phase === 'fallback' ? `当前模型不可用，切换到 ${modelStatus.nextModel || '备用模型'}...` : `模型响应异常，第 ${modelStatus.attempt} 次重试...`) : current.label}</span>
        <span className="font-mono text-muted-foreground">{current.pct}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-border">
        <div className="h-full rounded-full bg-indigo-500 transition-all duration-300" style={{ width: `${current.pct}%` }} />
      </div>
    </div>
  )
}

function EmptyAnalysisState() {
  const { t } = usePreferences()
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center text-muted-foreground animate-fade-in-up">
      <div className="w-full max-w-sm rounded-2xl border border-border/70 bg-card/45 p-8 text-center shadow-sm hover:border-primary/10 transition-colors">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-xl">📊</div>
        <p className="text-sm font-semibold text-foreground">{t('analysis.emptyTitle')}</p>
        <p className="mt-1.5 text-xs text-muted-foreground leading-relaxed">{t('analysis.emptySubtitle')}</p>
      </div>
    </div>
  )
}

async function resolveAnalysisCode(rawInput: string, selected: StockSearchResult | null): Promise<{ code: string; stock: StockSearchResult | null } | null> {
  const raw = rawInput.trim()
  const stock = matchesSelectedQuery(raw, selected, 'analysis') ? selected : await resolveStockQuery(raw)
  const code = stock?.analysisCode || (/^\d+$/.test(raw) ? raw : raw.toUpperCase())
  return isSupportedKlineCode(code) ? { code, stock } : null
}

async function fetchStockName(code: string): Promise<{ data: { name?: string } | null }> {
  if (!isCnSymbol(code)) return { data: null }
  const { data } = await supabase.from('recommendation_tracking').select('name').eq('code', parseInt(code, 10)).limit(1).single()
  return { data }
}

function buildKlinePayload(data: KlineRow[], quality: KlineDataQuality, contextPack?: AnalysisContextPack): string {
  const last = data[data.length - 1]!
  const prev20 = data.slice(-20)
  const ma5 = avg(data.slice(-5).map((d) => d.close))
  const ma20 = avg(prev20.map((d) => d.close))
  const ma50 = data.length >= 50 ? avg(data.slice(-50).map((d) => d.close)) : 0

  const summary = [
    `日线数据摘要（前复权，共${data.length}根，按日期升序；来源=${quality.source}；最新交易日=${quality.latestTradingDate || '未知'}）：`,
    `最新收盘：${last.close.toFixed(2)}`,
    `MA5=${ma5.toFixed(2)} MA20=${ma20.toFixed(2)}${ma50 ? ` MA50=${ma50.toFixed(2)}` : ''}`,
    `近20日最高：${Math.max(...prev20.map((d) => d.high)).toFixed(2)}`,
    `近20日最低：${Math.min(...prev20.map((d) => d.low)).toFixed(2)}`,
    `近5日平均量：${avg(data.slice(-5).map((d) => d.volume)).toFixed(0)}`,
    `近20日平均量：${avg(prev20.map((d) => d.volume)).toFixed(0)}`,
  ].join('\n')
  const csvRows = data.map((d) => [d.date, d.open.toFixed(2), d.high.toFixed(2), d.low.toFixed(2), d.close.toFixed(2), Math.round(d.volume)].join(','))

  return [
    summary, '',
    contextPack ? formatAnalysisContextPack(contextPack) : '',
    '',
    '以下是近320个交易日以内的完整日线OHLCV CSV数据。你必须读取这些数据进行判断，不要声称无法读取日线数据。',
    '```csv', 'date,open,high,low,close,volume', ...csvRows, '```',
  ].join('\n')
}

export const ANALYSIS_SYSTEM_PROMPT = `你是威科夫分析大师，主框架是量价与威科夫阶段判断。若用户提供价值面摘要，只把它作为质量、风险和仓位置信度校准：技术面负责时机，价值面负责是否值得提高/降低结论置信度。不要用基本面替代 K 线事实，也不要因为单个指标给出过度确定结论。

【核心质量要求】
- 必须在报告中说明数据来源（如 TickFlow / Tushare）、给出明确结论的置信度理由（如“置信度：80%，理由是...”），并提供明确的Plan B/策略失效位与风险提示。
- 严禁在结论中使用“必然”、“保证”、“无风险”、“稳赚”、“稳赢”、“包赚”等夸大或确定性的承诺词语。

输出结构：
1. 技术面结论：威科夫阶段、量价供需、支撑阻力、主力意图。
2. 价值面校准：只引用给定摘要中的关键指标，说明它如何影响风险/置信度。
3. 综合策略：观察/试错/持有/减仓等动作条件，包含失效位和风险提示。

请用简洁、专业的中文 markdown 回答。`

async function callLLM(configs: Parameters<typeof streamLLMResponseWithFallback>[0], code: string, name: string, klinePayload: string, valueSnapshot: ValueSnapshot, signal?: AbortSignal, onDelta?: (chunk: string) => void, onStatus?: (status: LLMStreamStatus) => void): Promise<string> {
  const result = await streamLLMResponseWithFallback(configs, [
    { role: 'system', content: ANALYSIS_SYSTEM_PROMPT },
    { role: 'user', content: `请分析股票 ${code} ${name}。\n\n${buildValuePrompt(valueSnapshot)}\n\n${klinePayload}` },
  ], { temperature: 0.7, signal, onDelta, onStatus })
  if (!result) throw new Error('模型未返回结果，请重试')
  return result
}
