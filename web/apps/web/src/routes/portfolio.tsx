import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2, LayoutDashboard, Plus, Save, Trash2 } from 'lucide-react'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'
import { WyckoffLoading } from '@/components/loading'
import { usePreferences } from '@/lib/preferences'
import { loadLLMConfigCandidates } from '@/lib/chat-agent'
import { streamLLMResponseWithFallback } from '@/lib/llm-stream'
import { clearStreamFlush, scheduleStreamFlush } from '@/lib/stream-render'
import { MarkdownContent } from '@/components/markdown'
import { UpgradeNotice } from '@/components/upgrade-notice'
import { AIDisclaimer } from '@/components/ai-disclaimer'
import { TICKFLOW_PURCHASE, fetchValueSnapshotWithFetch, isSupportedPortfolioCode, normalizeCode, normalizePortfolioCode } from '@wyckoff/shared'
import type { KlineRow, ValueSnapshot } from '@wyckoff/shared'
import { fetchKlineViaTickFlow, getUserDataKeys } from '@/lib/kline'
import { formatSignedPercent } from '@/lib/format'
import { useWhitelistGate } from '@/lib/whitelist-gate'
import { avg } from '@/lib/math'
import { EMPTY_PORTFOLIO, requestPortfolio, type Portfolio, type Position } from '@/lib/portfolio-api'
import { saveAnalysisHistory } from '@/lib/local-history'
import { sourceLabel, VALUE_RULESET_VERSION, valueTraceMeta, type ValueScore, type ValueTone } from '@wyckoff/shared'
import { buildValueDigest, buildValueScore, formatValuePercent, metricToneClass, numberTone, reverseNumberTone, signalClass, sortByValueRisk, valueDataQualityText, valueDataQualityTitle, valueScoreClass, valueUnavailableText, type ValueView } from '@/lib/value-analysis'

interface PositionPnL {
  code: string
  name: string
  shares: number
  cost: number
  latest: number
  costVal: number
  mktVal: number
  pnlPct: number
  weight: number
}

interface FullDiagnosisResult {
  report: string
  positions: PositionPnL[]
  values: PortfolioValueRow[]
  klineRows: number
  summaryStats: { totalCost: number; totalMarket: number; pnlPct: number; freeCash: number; count: number }
}

interface PortfolioValueRow {
  code: string
  name: string
  snapshot: ValueSnapshot
}

interface PortfolioHistoryPayload {
  source: 'database' | 'manual'
  result: FullDiagnosisResult
  report: string
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

async function portfolioApi(method: 'GET' | 'PUT', portfolio?: Portfolio): Promise<Portfolio> {
  const { data: { session } } = await supabase.auth.getSession()
  if (!session?.access_token) throw new Error('登录已失效，请重新登录')
  return requestPortfolio(method, session.access_token, portfolio)
}

export function PortfolioPage() {
  const userId = useAuthStore((s) => s.user?.id)
  return <PortfolioPageContent key={userId || 'anonymous'} />
}

function PortfolioPageContent() {
  const user = useAuthStore((s) => s.user)
  const portfolioData = usePortfolioData(user?.id)
  const fullDiag = useFullDiagnosisRunner()
  const [manualPortfolio, setManualPortfolio] = useState<Portfolio>(EMPTY_PORTFOLIO)
  const [databaseDraft, setDatabaseDraft] = useState<Portfolio>(EMPTY_PORTFOLIO)
  const source = portfolioData.isWhitelisted ? 'database' : 'manual'
  usePortfolioHistory(user?.id, fullDiag.result, source, fullDiag.model)

  useEffect(() => {
    if (portfolioData.isWhitelisted && portfolioData.portfolio) setDatabaseDraft(portfolioData.portfolio)
  }, [portfolioData.isWhitelisted, portfolioData.portfolio])

  if (portfolioData.isLoading) return <WyckoffLoading />

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5 p-6">
      <PageHeader />
      {portfolioData.loadError && <UpgradeNotice message={portfolioData.loadError} />}
      {fullDiag.error && <UpgradeNotice message={fullDiag.error} />}
      {portfolioData.isWhitelisted ? (
        <ManualInput
          portfolio={databaseDraft}
          fullLoading={fullDiag.loading}
          progress={fullDiag.progress}
          onChange={(draft) => { portfolioData.resetSave(); setDatabaseDraft(draft) }}
          onDiagnosis={() => { void portfolioData.save(databaseDraft).then((saved) => fullDiag.run(saved)).catch(() => undefined) }}
          onSave={() => { void portfolioData.save(databaseDraft).catch(() => undefined) }}
          saving={portfolioData.isSaving}
          saveError={portfolioData.saveError}
          saveSuccess={portfolioData.saveSuccess}
          databaseMode
        />
      ) : (
        <ManualInput portfolio={manualPortfolio} fullLoading={fullDiag.loading} progress={fullDiag.progress} onChange={setManualPortfolio} onDiagnosis={() => fullDiag.run(manualPortfolio)} />
      )}
      {fullDiag.result && <FullDiagnosisPanel result={fullDiag.result} report={fullDiag.streamingReport || fullDiag.result.report} streaming={fullDiag.loading && fullDiag.progress?.step === 'llm'} />}
    </div>
  )
}

function usePortfolioData(userId: string | undefined) {
  const whitelist = useWhitelistGate(userId)
  const portfolio = useQuery({
    queryKey: ['portfolio', userId],
    queryFn: () => portfolioApi('GET'),
    enabled: !!userId && whitelist.data === true,
  })
  const saveMutation = useMutation({
    mutationFn: (draft: Portfolio) => portfolioApi('PUT', draft),
    onSuccess: (saved) => portfolio.refetch().then(() => saved),
  })
  const isWhitelisted = whitelist.data === true
  return {
    isWhitelisted,
    isLoading: whitelist.isLoading || (isWhitelisted && portfolio.isLoading),
    portfolio: portfolio.data || EMPTY_PORTFOLIO,
    loadError: portfolio.error instanceof Error ? portfolio.error.message : '',
    save: (draft: Portfolio) => saveMutation.mutateAsync(draft),
    isSaving: saveMutation.isPending,
    saveError: saveMutation.error instanceof Error ? saveMutation.error.message : '',
    saveSuccess: saveMutation.isSuccess,
    resetSave: () => saveMutation.reset(),
  }
}

interface DiagProgress {
  step: 'config' | 'kline' | 'llm'
  fetched: number
  total: number
}

function useFullDiagnosisRunner() {
  const user = useAuthStore((s) => s.user)
  const { t } = usePreferences()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<FullDiagnosisResult | null>(null)
  const [streamingReport, setStreamingReport] = useState('')
  const [model, setModel] = useState('unknown')
  const [progress, setProgress] = useState<DiagProgress | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const streamBuf = useRef('')
  const flushTimer = useRef<ReturnType<typeof setTimeout> | 0>(0)

  async function run(portfolio: Portfolio) {
    if (!user || loading || portfolio.positions.length === 0) return
    const total = portfolio.positions.length
    const abort = startDiagnosisRun(abortRef, streamBuf, flushTimer)
    resetDiagnosisState(setError, setResult, setStreamingReport, setLoading, setProgress, total)
    try {
      const [configs, keys] = await Promise.all([loadLLMConfigCandidates(user.id), getUserDataKeys(user.id)])
      if (configs.length === 0) throw new Error(t('portfolio.missingModel'))
      setModel(configs[0]?.model || 'unknown')

      setProgress({ step: 'kline', fetched: 0, total })
      const entries = await fetchAllPositionKlines(portfolio.positions, keys, (n) => setProgress({ step: 'kline', fetched: n, total }))
      if (abort.signal.aborted) return

      setResult(buildDiagnosisResult(entries, '', portfolio.free_cash))
      setProgress({ step: 'llm', fetched: total, total })
      const prompt = buildFullPortfolioPrompt(entries, portfolio.free_cash)
      const onDelta = (chunk: string) => {
        streamBuf.current += chunk
        scheduleStreamFlush(streamBuf, flushTimer, setStreamingReport)
      }
      const report = await callFullPortfolioLLM(configs, prompt, abort.signal, onDelta, (nextModel) => setModel(nextModel))
      clearStreamFlush(flushTimer)
      if (abort.signal.aborted) return
      setStreamingReport(report)
      setResult(buildDiagnosisResult(entries, report, portfolio.free_cash))
    } catch (err) {
      if (abort.signal.aborted) return
      setError(err instanceof Error ? err.message : t('portfolio.failed'))
    } finally {
      finishDiagnosisRun(flushTimer, setLoading, setProgress)
    }
  }

  return { loading, error, result, streamingReport, progress, run, model }
}

function usePortfolioHistory(userId: string | undefined, result: FullDiagnosisResult | null, source: PortfolioHistoryPayload['source'], model: string) {
  const savedKey = useRef('')

  useEffect(() => {
    if (!userId || !result?.report) return

    const rawText = `${VALUE_RULESET_VERSION}:${result.positions.map(p => {
      const val = result.values.find(v => v.code === p.code);
      const src = val?.snapshot.source || '';
      const metricsJson = val?.snapshot.metrics ? JSON.stringify(val.snapshot.metrics) : 'none';
      return `${p.code}:${p.shares}:${p.cost}:${src}:${metricsJson}`;
    }).join('|')}`;
    let hash = 2166136261;
    for (let i = 0; i < rawText.length; i++) {
      hash = Math.imul(hash ^ rawText.charCodeAt(i), 16777619);
    }
    const inputSnapshotHash = (hash >>> 0).toString(16);
    const valueTraces = result.values.map(value => valueTraceMeta(value.snapshot))

    const meta = {
      inputSnapshotHash,
      promptVersion: 'wyckoff-prompt-v2.1',
      model,
      generatedAt: new Date().toISOString(),
      valueSource: result.values.map(v => sourceLabel(v.snapshot)).filter(Boolean).join(','),
      reportDate: result.values.map(v => v.snapshot.metrics?.period_end || 'unknown').filter(Boolean).join(','),
      valueRulesetVersion: VALUE_RULESET_VERSION,
      valueDataQuality: valueTraces.map(trace => trace.dataQuality).join(','),
      valueRuleCodes: [...new Set(valueTraces.flatMap(trace => trace.ruleCodes))],
      klineRows: result.klineRows || undefined,
    }

    const payload: PortfolioHistoryPayload = { source, result, report: result.report, meta }
    const key = portfolioHistoryKey(payload)
    if (savedKey.current === key) return
    savedKey.current = key
    void saveAnalysisHistory({
      kind: 'portfolio-diagnosis',
      userId,
      title: `${result.summaryStats.count}只持仓诊断`,
      subtitle: `${source === 'database' ? '数据库持仓' : '手动持仓'} · ${formatSignedPercent(result.summaryStats.pnlPct)}`,
      symbols: result.positions.map((position) => position.code),
      payload,
    }).catch(() => undefined)
  }, [result, source, userId, model])
}

function portfolioHistoryKey(payload: PortfolioHistoryPayload): string {
  return `${payload.source}:${payload.result.positions.map((position) => position.code).join(',')}:${payload.report.length}`
}

function startDiagnosisRun(
  abortRef: MutableRefObject<AbortController | null>,
  streamBuf: MutableRefObject<string>,
  flushTimer: MutableRefObject<ReturnType<typeof setTimeout> | 0>,
) {
  abortRef.current?.abort()
  clearStreamFlush(flushTimer)
  const abort = new AbortController()
  abortRef.current = abort
  streamBuf.current = ''
  return abort
}

function resetDiagnosisState(
  setError: Dispatch<SetStateAction<string>>,
  setResult: Dispatch<SetStateAction<FullDiagnosisResult | null>>,
  setStreamingReport: Dispatch<SetStateAction<string>>,
  setLoading: Dispatch<SetStateAction<boolean>>,
  setProgress: Dispatch<SetStateAction<DiagProgress | null>>,
  total: number,
) {
  setError('')
  setResult(null)
  setStreamingReport('')
  setLoading(true)
  setProgress({ step: 'config', fetched: 0, total })
}

function finishDiagnosisRun(
  flushTimer: MutableRefObject<ReturnType<typeof setTimeout> | 0>,
  setLoading: Dispatch<SetStateAction<boolean>>,
  setProgress: Dispatch<SetStateAction<DiagProgress | null>>,
) {
  clearStreamFlush(flushTimer)
  setLoading(false)
  setProgress(null)
}

type PositionEntry = { position: Position; kline: KlineRow[]; valueSnapshot: ValueSnapshot }

async function fetchAllPositionKlines(positions: Position[], keys: Awaited<ReturnType<typeof getUserDataKeys>>, onProgress: (n: number) => void): Promise<PositionEntry[]> {
  if (!keys.tickflow) throw new Error(`触发数据源并发请求限制，请升级数据源：${TICKFLOW_PURCHASE}`)
  let fetched = 0
  const entries: PositionEntry[] = []
  const errors: string[] = []
  await Promise.all(
    positions.map(async (p, i) => {
      try {
        const code = normalizePortfolioCode(p.code) || normalizeCode(p.code)
        const [kline, valueSnapshot] = await Promise.all([
          fetchKlineViaTickFlow(code, keys.tickflow!),
          fetchValueSnapshotWithFetch(globalThis.fetch, code, keys).catch((): ValueSnapshot => ({ symbol: code, source: 'none', metrics: null, reason: 'not-found' })),
        ])
        if (kline.length > 0) entries.push({ position: positions[i]!, kline, valueSnapshot })
        else errors.push(code)
      } catch (err) {
        errors.push(`${normalizePortfolioCode(p.code) || normalizeCode(p.code)}: ${err instanceof Error ? err.message : '失败'}`)
      }
      onProgress(++fetched)
    }),
  )
  if (errors.length > 0) throw new Error(`K 线获取失败: ${errors.join(', ')}`)
  if (entries.length === 0) throw new Error('无法获取任何持仓的 K 线数据')
  entries.sort((a, b) => positions.indexOf(a.position) - positions.indexOf(b.position))
  return entries
}

function buildDiagnosisResult(entries: PositionEntry[], report: string, freeCash: number): FullDiagnosisResult {
  const totalCost = entries.reduce((s, e) => s + Number(e.position.shares || 0) * Number(e.position.cost_price || 0), 0)
  const totalMarket = entries.reduce((s, e) => s + Number(e.position.shares || 0) * (e.kline[e.kline.length - 1]?.close || 0), 0)
  const positions: PositionPnL[] = entries.map((e) => {
    const shares = Number(e.position.shares || 0)
    const cost = Number(e.position.cost_price || 0)
    const latest = e.kline[e.kline.length - 1]?.close || 0
    const costVal = shares * cost
    const mktVal = shares * latest
    return {
      code: normalizeCode(e.position.code), name: e.position.name || normalizeCode(e.position.code),
      shares, cost, latest, costVal, mktVal,
      pnlPct: costVal > 0 ? ((mktVal - costVal) / costVal) * 100 : 0,
      weight: totalMarket > 0 ? (mktVal / totalMarket) * 100 : 0,
    }
  })
  const values: PortfolioValueRow[] = entries.map((e) => {
    const code = normalizeCode(e.position.code)
    return { code, name: e.position.name || code, snapshot: e.valueSnapshot }
  })
  return {
    report, positions, values,
    klineRows: entries.reduce((sum, entry) => sum + entry.kline.length, 0),
    summaryStats: { totalCost, totalMarket, pnlPct: totalCost > 0 ? ((totalMarket - totalCost) / totalCost) * 100 : 0, freeCash, count: entries.length },
  }
}

function PageHeader() {
  const { t } = usePreferences()
  return (
    <header className="border-b border-border pb-5">
      <h1 className="text-xl font-semibold">{t('portfolio.title')}</h1>
      <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{t('portfolio.fullDiagnosisHint')}</p>
    </header>
  )
}

function ManualInput({
  portfolio, fullLoading, progress, onChange, onDiagnosis, onSave, saving = false, saveError = '', saveSuccess = false, databaseMode = false,
}: {
  portfolio: Portfolio; fullLoading: boolean; progress: DiagProgress | null
  onChange: (p: Portfolio) => void; onDiagnosis: () => void
  onSave?: () => void; saving?: boolean; saveError?: string; saveSuccess?: boolean; databaseMode?: boolean
}) {
  const { t } = usePreferences()
  const addPosition = () => onChange({ ...portfolio, positions: [...portfolio.positions, { code: '', name: null, shares: 0, cost_price: 0, buy_dt: null }] })
  const removePosition = (i: number) => onChange({ ...portfolio, positions: portfolio.positions.filter((_, idx) => idx !== i) })
  const updatePosition = (i: number, patch: Partial<Position>) => onChange({ ...portfolio, positions: portfolio.positions.map((p, idx) => idx === i ? { ...p, ...patch } : p) })
  const canDiagnose = portfolio.positions.length > 0 && portfolio.positions.every(isValidManualPosition)

  return (
    <section className="rounded-lg border border-border">
      <div className="flex items-center justify-between border-b border-border bg-muted/30 p-4">
        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1">
            <label className="text-sm font-medium">{t('portfolio.freeCash')}</label>
            <input type="number" min={0} value={portfolio.free_cash || ''} onChange={(e) => onChange({ ...portfolio, free_cash: Number(e.target.value) || 0 })} className="block w-40 rounded-md border border-border bg-background px-3 py-1.5 text-sm outline-none" placeholder="0" />
          </div>
          {databaseMode && portfolio.total_equity != null && (
            <div className="pb-1 text-sm">
              <div className="text-xs text-muted-foreground">{t('portfolio.totalEquity')}</div>
              <div className="font-semibold">¥{portfolio.total_equity.toLocaleString()}</div>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {databaseMode && onSave && (
            <button type="button" disabled={saving || fullLoading || !canDiagnose} onClick={onSave} className="inline-flex items-center gap-2 rounded-lg border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50">
              {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              {saving ? t('portfolio.saving') : t('portfolio.save')}
            </button>
          )}
          <button type="button" disabled={fullLoading || saving || !canDiagnose} onClick={onDiagnosis} className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            {fullLoading || saving ? <Loader2 size={16} className="animate-spin" /> : <LayoutDashboard size={16} />}
            {fullLoading ? t('portfolio.fullLoading') : databaseMode ? t('portfolio.saveAndDiagnose') : t('portfolio.fullDiagnosis')}
          </button>
        </div>
      </div>
      {saveError && <div className="border-b border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{saveError}</div>}
      {portfolio.valuation_warning && <div className="border-b border-amber-500/30 bg-amber-500/5 px-4 py-2 text-sm text-amber-700 dark:text-amber-300">{portfolio.valuation_warning}</div>}
      {saveSuccess && !saveError && <div className="border-b border-emerald-500/30 bg-emerald-500/5 px-4 py-2 text-sm text-emerald-700 dark:text-emerald-300">{t('portfolio.saved')}</div>}
      {progress && <DiagProgressBar progress={progress} />}
      <div className="divide-y divide-border">
        {portfolio.positions.map((pos, i) => (
          <ManualPositionRow key={i} position={pos} onChange={(patch) => updatePosition(i, patch)} onRemove={() => removePosition(i)} />
        ))}
      </div>
      <button type="button" onClick={addPosition} className="flex w-full items-center justify-center gap-2 border-t border-border py-3 text-sm text-muted-foreground hover:bg-muted/30">
        <Plus size={14} /> {t('portfolio.addPosition')}
      </button>
    </section>
  )
}

function ManualPositionRow({ position, onChange, onRemove }: { position: Position; onChange: (patch: Partial<Position>) => void; onRemove: () => void }) {
  const { t } = usePreferences()
  const cls = 'rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none'
  return (
    <div className="flex flex-wrap items-center gap-3 px-4 py-3">
      <input value={String(position.code)} onChange={(e) => onChange({ code: e.target.value })} placeholder={t('portfolio.code')} className={`${cls} w-36`} title={t('portfolio.invalidCode')} />
      <input value={position.name || ''} onChange={(e) => onChange({ name: e.target.value || null })} placeholder={t('portfolio.name')} className={`${cls} w-24`} />
      <input type="number" min={0} value={position.shares || ''} onChange={(e) => onChange({ shares: Number(e.target.value) || 0 })} placeholder={t('portfolio.shares')} className={`${cls} w-20`} />
      <input type="number" min={0} step={0.01} value={position.cost_price || ''} onChange={(e) => onChange({ cost_price: Number(e.target.value) || 0 })} placeholder={t('portfolio.costPrice')} className={`${cls} w-24`} />
      <input type="date" value={position.buy_dt || ''} onChange={(e) => onChange({ buy_dt: e.target.value || null })} className={`${cls} w-36`} aria-label={t('portfolio.buyDate')} />
      <button type="button" onClick={onRemove} className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"><Trash2 size={14} /></button>
    </div>
  )
}

function isValidManualPosition(position: Position): boolean {
  return isSupportedPortfolioCode(position.code) && Number(position.shares) > 0 && Number(position.cost_price) > 0
}

function DiagProgressBar({ progress }: { progress: DiagProgress }) {
  const { step, fetched, total } = progress
  let pct: number
  let label: string
  if (step === 'config') {
    pct = 5
    label = '加载配置...'
  } else if (step === 'kline') {
    pct = 5 + (total > 0 ? (fetched / total) * 70 : 0)
    label = `拉取 K 线 ${fetched}/${total}`
  } else {
    pct = 80
    label = '等待模型分析...'
  }
  return (
    <div className="border-b border-border bg-muted/10 px-4 py-2.5">
      <div className="mb-1.5 flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono text-muted-foreground">{Math.round(pct)}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-border">
        <div className="h-full rounded-full bg-indigo-500 transition-all duration-300" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function FullDiagnosisPanel({ result, report, streaming }: { result: FullDiagnosisResult; report: string; streaming: boolean }) {
  const { t } = usePreferences()
  const { summaryStats: s } = result
  return (
    <section className="space-y-4">
      <PnLTable positions={result.positions} stats={s} />
      <PortfolioValuePanel values={result.values} />
      <div className="rounded-lg border border-indigo-200 bg-indigo-50/30 p-5 dark:border-indigo-500/30 dark:bg-indigo-500/5">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          {streaming ? <Loader2 size={18} className="animate-spin text-indigo-600 dark:text-indigo-400" /> : <LayoutDashboard size={18} className="text-indigo-600 dark:text-indigo-400" />}
          <h2 className="text-base font-semibold">{t('portfolio.fullDiagnosis')}</h2>
        </div>
        <AIDisclaimer />
        <article className="mt-4 prose prose-sm max-w-none text-foreground">
          {report ? <MarkdownContent content={report} streaming={streaming} /> : <p className="text-sm text-muted-foreground">模型分析中...</p>}
        </article>
      </div>
    </section>
  )
}

function PortfolioValuePanel({ values }: { values: PortfolioValueRow[] }) {
  const { t } = usePreferences()
  const [view, setView] = useState<ValueView>('quality')
  if (values.length === 0) return null
  const rows = sortByValueRisk(values, v => v.snapshot.metrics)
  return (
    <section className="rounded-lg border border-border p-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{t('analysis.valueTitle')}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{t('analysis.valueSubtitle')}</p>
        </div>
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
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {rows.map((row) => <PortfolioValueCard key={row.code} row={row} view={view} />)}
      </div>
    </section>
  )
}

function PortfolioValueCard({ row, view }: { row: PortfolioValueRow; view: ValueView }) {
  const { t } = usePreferences()
  const metrics = row.snapshot.metrics
  const value = buildValueScore(metrics, t)
  if (!metrics) {
    return (
      <div className="rounded-lg border border-border p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{row.code} {row.name}</h3>
            <p className="mt-1 text-xs text-muted-foreground">{valueUnavailableText(row.snapshot.reason, t)}</p>
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
          <h3 className="truncate text-sm font-semibold">{row.code} {row.name}</h3>
          <p title={valueDataQualityTitle(row.snapshot, t)} className="mt-1 text-xs text-muted-foreground">{sourceLabel(row.snapshot)}{metrics.period_end ? ` · ${metrics.period_end}` : ''} · {valueDataQualityText(row.snapshot, t)}</p>
        </div>
        <ValueBadge value={value} />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-sm md:grid-cols-3">
        <ValueMetricCell label={t('analysis.valueRoe')} value={formatValuePercent(metrics.roe)} tone={numberTone(metrics.roe, 10, 0)} />
        <ValueMetricCell label={t('analysis.valueProfitYoy')} value={formatValuePercent(metrics.net_income_yoy)} tone={numberTone(metrics.net_income_yoy, 0, -10)} />
        <ValueMetricCell label={t('analysis.valueRevenueYoy')} value={formatValuePercent(metrics.revenue_yoy)} tone={numberTone(metrics.revenue_yoy, 0, -10)} />
        <ValueMetricCell label={t('analysis.valueGrossMargin')} value={formatValuePercent(metrics.gross_margin)} tone={numberTone(metrics.gross_margin, 30, 15)} />
        <ValueMetricCell label={t('analysis.valueDebtRatio')} value={formatValuePercent(metrics.debt_to_asset_ratio)} tone={reverseNumberTone(metrics.debt_to_asset_ratio, 55, 70)} />
        <ValueMetricCell label={t('analysis.valueCashRevenue')} value={formatValuePercent(metrics.operating_cash_to_revenue)} tone={numberTone(metrics.operating_cash_to_revenue, 5, 0)} />
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

function ValueMetricCell({ label, value, tone }: { label: string; value: string; tone: ValueTone }) {
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

function PnLTable({ positions, stats }: { positions: PositionPnL[]; stats: FullDiagnosisResult['summaryStats'] }) {
  const totalAssets = stats.totalMarket + stats.freeCash
  const cashWeight = totalAssets > 0 ? (stats.freeCash / totalAssets) * 100 : 0
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead className="bg-muted/40">
          <tr>
            {['代码', '名称', '股数', '成本', '现价', '市值', '浮盈', '仓位'].map((h) => (
              <th key={h} scope="col" className="px-3 py-2 text-left font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.code} className="border-t border-border">
              <td className="px-3 py-2 font-mono">{p.code}</td>
              <td className="px-3 py-2">{p.name}</td>
              <td className="px-3 py-2 text-right">{p.shares.toLocaleString()}</td>
              <td className="px-3 py-2 text-right">¥{p.cost.toFixed(2)}</td>
              <td className="px-3 py-2 text-right">¥{p.latest.toFixed(2)}</td>
              <td className="px-3 py-2 text-right">¥{p.mktVal.toLocaleString()}</td>
              <td className={`px-3 py-2 text-right font-medium ${p.pnlPct >= 0 ? 'text-up' : 'text-down'}`}>{formatSignedPercent(p.pnlPct)}</td>
              <td className="px-3 py-2 text-right">{p.weight.toFixed(1)}%</td>
            </tr>
          ))}
          <tr className="border-t-2 border-border bg-muted/20 font-medium">
            <td className="px-3 py-2" colSpan={5}>合计</td>
            <td className="px-3 py-2 text-right">¥{stats.totalMarket.toLocaleString()}</td>
            <td className={`px-3 py-2 text-right ${stats.pnlPct >= 0 ? 'text-up' : 'text-down'}`}>{formatSignedPercent(stats.pnlPct)}</td>
            <td className="px-3 py-2 text-right">100%</td>
          </tr>
          <tr className="border-t border-border text-muted-foreground">
            <td className="px-3 py-2" colSpan={5}>现金</td>
            <td className="px-3 py-2 text-right">¥{stats.freeCash.toLocaleString()}</td>
            <td className="px-3 py-2" />
            <td className="px-3 py-2 text-right">{cashWeight.toFixed(1)}%</td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}

function buildFullPortfolioPrompt(entries: PositionEntry[], freeCash: number): string {
  const totalCost = entries.reduce((s, e) => s + Number(e.position.shares || 0) * Number(e.position.cost_price || 0), 0)
  const totalMarket = entries.reduce((s, e) => s + Number(e.position.shares || 0) * (e.kline[e.kline.length - 1]?.close || 0), 0)

  const sections = entries.map(({ position, kline, valueSnapshot }) => {
    const code = normalizeCode(position.code)
    const shares = Number(position.shares || 0)
    const cost = Number(position.cost_price || 0)
    const latest = kline[kline.length - 1]!.close
    const costVal = shares * cost
    const mktVal = shares * latest
    const pnlPct = costVal > 0 ? ((mktVal - costVal) / costVal) * 100 : 0
    const weight = totalCost > 0 ? ((costVal / totalCost) * 100).toFixed(1) : '0'
    const recent = kline.slice(-60)
    const ma5 = avg(kline.slice(-5).map((d) => d.close))
    const ma20 = avg(kline.slice(-20).map((d) => d.close))
    const csv = recent.map((d) => [d.date, d.close.toFixed(2), Math.round(d.volume)].join(',')).join('\n')
    return [
      `## ${code} ${position.name || code}`,
      `${shares}股 成本¥${cost.toFixed(2)} 最新¥${latest.toFixed(2)} 浮盈${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}% 仓位占比${weight}%`,
      `MA5=${ma5.toFixed(2)} MA20=${ma20.toFixed(2)} 60日高=${Math.max(...recent.map((d) => d.high)).toFixed(2)} 60日低=${Math.min(...recent.map((d) => d.low)).toFixed(2)}`,
      buildValueDigest(valueSnapshot),
      '```csv\ndate,close,volume', csv, '```',
    ].join('\n')
  })

  const totalPnl = totalCost > 0 ? ((totalMarket - totalCost) / totalCost) * 100 : 0
  const totalAssets = totalMarket + freeCash
  const cashPct = totalAssets > 0 ? (freeCash / totalAssets) * 100 : 0

  const header = [
    `# 账户概况`,
    `现金 ¥${freeCash.toLocaleString()}（${cashPct.toFixed(1)}%）| 持仓 ${entries.length} 只 | 总成本 ¥${totalCost.toLocaleString()} | 总市值 ¥${totalMarket.toLocaleString()} | 整体盈亏 ${formatSignedPercent(totalPnl)}`,
  ].join('\n')

  return [header, '', ...sections].join('\n\n')
}

export const PORTFOLIO_SYSTEM_PROMPT = `你是威科夫资产配置诊断专家。基于用户的全部持仓、真实K线和价值面快照做整体诊断。主框架仍是仓位、趋势和量价结构；价值面只用于校准公司质量、风险暴露和仓位置信度，不要用基本面替代K线事实。

【核心质量要求】
- 必须在诊断报告中说明数据来源、给出明确的置信度理由与配置风控建议，并提供调仓或防守策略的失效条件/警戒水位线。
- 绝对禁止在分析结论中使用“必然”、“保证”、“无风险”、“稳赚”、“稳赢”、“包赚”等夸大或确定性的承诺词语。

输出包含：
1. 仓位分布评估（集中度、行业分散性）
2. 各持仓当前威科夫阶段一句话判断，并说明价值面质量/风险如何影响持仓置信度
3. 现金比例是否合理
4. 整体风险暴露（哪些持仓需要警惕，包括失效位和风险提示）
5. 加减仓优先级建议
6. 操作建议（先减谁、可加谁、现金该不该动）

用简洁的 Markdown 格式回答。不编造数据。`

async function callFullPortfolioLLM(configs: Parameters<typeof streamLLMResponseWithFallback>[0], prompt: string, signal?: AbortSignal, onDelta?: (chunk: string) => void, onModel?: (model: string) => void): Promise<string> {
  const result = await streamLLMResponseWithFallback(configs, [
    { role: 'system', content: PORTFOLIO_SYSTEM_PROMPT },
    { role: 'user', content: `请对我的完整持仓做整体诊断和资产配置建议。\n\n${prompt}` },
  ], { temperature: 0.5, maxTokens: 4000, signal, onDelta, onStatus: (status) => onModel?.(status.nextModel || status.model) })
  if (!result) throw new Error('模型未返回结果，请重试')
  return result
}
