import { useCallback, useState, useMemo, useRef, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, CheckCircle2, ExternalLink, ShieldCheck } from 'lucide-react'
import { createChart, HistogramSeries, type Time } from 'lightweight-charts'
import { supabase } from '@/lib/supabase'
import { watchChartResize } from '@/lib/chart-resize'
import { useWhitelistGate, whitelistGateView } from '@/lib/whitelist-gate'
import { SortableHeader, type SortOrder } from '@/components/sortable-header'
import { dedupeTrackingRows, labelCandidateTerm, normalizeCode } from '@wyckoff/shared'
import { WyckoffLoading } from '@/components/loading'
import { usePreferences, type TranslationKey } from '@/lib/preferences'
import { financialValueClass } from '@/lib/financial-colors'
import { formatSignedPercent, isFiniteNumber } from '@/lib/format'
import { useAuthStore } from '@/stores/auth'

type MarketTab = 'cn' | 'us' | 'hk'

const MARKET_TABLE: Record<MarketTab, string> = {
  cn: 'recommendation_tracking',
  us: 'recommendation_tracking_us',
  hk: 'recommendation_tracking_hk',
}

interface Recommendation {
  code: number | string
  name: string | null
  recommend_date: number
  initial_price: number | null
  current_price: number | null
  change_pct: number | null
  mfe_pct?: number | null
  mae_pct?: number | null
  range_amp_pct?: number | null
  mfe_price?: number | null
  mae_price?: number | null
  performance_days?: number | null
  is_ai_recommended: boolean
  rag_vetoed: boolean
  funnel_score: number | null
  recommend_count: number | null
  recommend_reason: string | null
  springboard_a?: boolean | null
  springboard_b?: boolean | null
  springboard_c?: boolean | null
  springboard_combo?: string | null
  springboard_grade?: string | null
  springboard_met_count?: number | null
  springboard_scored?: boolean | null
  strategy_version?: string | null
  candidate_lane?: string | null
  entry_type?: string | null
  signal_key?: string | null
  candidate_status?: string | null
  action_status?: string | null
  action_label?: string | null
  action_level?: string | null
  direct_buy_allowed?: boolean | string | number | null
  candidate_timing?: string | null
  candidate_risk?: string | null
  mainline_score?: number | null
  theme_score?: number | null
  stock_role_score?: number | null
  quality_score?: number | null
  timing_score?: number | null
  source_type?: string | null
  signal_status?: string | null
  signal_type?: string | null
}

interface SignalPendingRow {
  code: number | string
  name?: string | null
  signal_type?: string | null
  signal_date?: string | null
  status?: string | null
  signal_score?: number | null
  snap_close?: number | null
  strategy_version?: string | null
  candidate_lane?: string | null
  entry_type?: string | null
  signal_key?: string | null
  candidate_status?: string | null
  action_status?: string | null
  action_label?: string | null
  action_level?: string | null
  direct_buy_allowed?: boolean | string | number | null
  mainline_score?: number | null
  theme_score?: number | null
  stock_role_score?: number | null
  quality_score?: number | null
  timing_score?: number | null
}

interface SummaryStats {
  count: number
  avg: number | null
  best: number | null
  worst: number | null
  totalRecommendations: number
}

interface TrackingReadyContentProps {
  activeDates: number[]
  activeOldestDate: number | null
  filtered: Recommendation[]
  latestDate: number | null
  market: MarketTab
  onlyAI: boolean
  search: string
  selectedWindow: RecommendationWindow
  sortBy: SortBy
  sortOrder: SortOrder
  stats: SummaryStats | null
  visibleData: Recommendation[]
  windowRows: Recommendation[]
  onOnlyAIChange: (value: boolean) => void
  onSearchChange: (value: string) => void
  onSelectedWindowChange: (value: RecommendationWindow) => void
  onSortByChange: (value: SortBy) => void
  onSortOrderChange: (value: SortOrder) => void
}

const RETENTION_DATES = 30
const TRACKING_PAGE_SIZE = 1000
const AVG_WINDOWS = [5, 10, 15, 20, 25, 30] as const
type RecommendationWindow = (typeof AVG_WINDOWS)[number]
type SortBy = 'date' | 'change' | 'score' | 'count' | 'mfe' | 'mae'

const LOCKED_BENEFITS = [
  {
    titleKey: 'tracking.locked.window',
    descKey: 'tracking.locked.windowDesc',
  },
  {
    titleKey: 'tracking.locked.signals',
    descKey: 'tracking.locked.signalsDesc',
  },
  {
    titleKey: 'tracking.locked.replay',
    descKey: 'tracking.locked.replayDesc',
  },
] satisfies { titleKey: TranslationKey; descKey: TranslationKey }[]

async function fetchTracking(market: MarketTab): Promise<Recommendation[]> {
  const rows: Recommendation[] = []
  let offset = 0
  while (true) {
    const batch = await fetchTrackingPage(market, offset)
    rows.push(...batch)
    if (batch.length < TRACKING_PAGE_SIZE || hasLoadedRetentionWindow(rows)) break
    offset += TRACKING_PAGE_SIZE
  }
  const combined = market === 'cn' ? rows.concat(await fetchSignalPendingTracking()) : rows
  const dateSet = new Set(getLatestRecommendDates(combined, RETENTION_DATES))
  return combined.filter((row) => dateSet.has(row.recommend_date))
}

async function fetchTrackingPage(market: MarketTab, offset: number): Promise<Recommendation[]> {
  const { data, error } = await supabase
    .from(MARKET_TABLE[market])
    .select('*')
    .order('recommend_date', { ascending: false })
    .order('code', { ascending: true })
    .range(offset, offset + TRACKING_PAGE_SIZE - 1)
  if (error) throw new Error(`${MARKET_TABLE[market]}: ${error.message}`)
  return (data || []).map((row) => ({ ...row, source_type: 'recommendation_tracking' }))
}

async function fetchSignalPendingTracking(): Promise<Recommendation[]> {
  const { data, error } = await supabase
    .from('signal_pending')
    .select(
      'code,name,signal_type,signal_date,status,signal_score,snap_close,strategy_version,candidate_lane,entry_type,signal_key,candidate_status,mainline_score,theme_score,stock_role_score,quality_score,timing_score',
    )
    .in('status', ['pending', 'confirmed'])
    .order('signal_date', { ascending: false })
    .limit(2000)
  if (error) throw new Error(`signal_pending: ${error.message}`)
  return (data || []).map(mapSignalPendingRow).filter((row): row is Recommendation => row !== null)
}

function mapSignalPendingRow(row: SignalPendingRow): Recommendation | null {
  const recommendDate = signalDateNumber(row.signal_date)
  if (!recommendDate) return null
  return {
    code: normalizeCode(row.code),
    name: row.name ?? null,
    recommend_date: recommendDate,
    initial_price: nullableNumber(row.snap_close),
    current_price: null,
    change_pct: null,
    is_ai_recommended: false,
    rag_vetoed: false,
    funnel_score: nullableNumber(row.signal_score),
    recommend_count: 1,
    recommend_reason: `signal_pending:${row.status || 'pending'}:${row.signal_type || '-'}`,
    strategy_version: row.strategy_version ?? null,
    candidate_lane: row.candidate_lane ?? row.signal_type ?? null,
    entry_type: row.entry_type ?? null,
    signal_key: row.signal_key ?? row.signal_type ?? null,
    candidate_status: row.candidate_status ?? row.status ?? null,
    action_status: row.action_status ?? signalActionStatus(row.status),
    action_label: row.action_label ?? null,
    action_level: row.action_level ?? null,
    direct_buy_allowed: false,
    mainline_score: nullableNumber(row.mainline_score),
    theme_score: nullableNumber(row.theme_score),
    stock_role_score: nullableNumber(row.stock_role_score),
    quality_score: nullableNumber(row.quality_score),
    timing_score: nullableNumber(row.timing_score),
    source_type: 'signal_pending',
    signal_status: row.status ?? null,
    signal_type: row.signal_type ?? null,
  }
}

function hasLoadedRetentionWindow(rows: Recommendation[]): boolean {
  const dates = getLatestRecommendDates(rows, RETENTION_DATES + 1)
  const cutoffDate = dates[RETENTION_DATES - 1]
  const oldestFetched = rows.at(-1)?.recommend_date
  return (
    dates.length > RETENTION_DATES
    && typeof oldestFetched === 'number'
    && typeof cutoffDate === 'number'
    && oldestFetched < cutoffDate
  )
}

export function TrackingPage() {
  const [market, setMarket] = useState<MarketTab>('cn')
  const [search, setSearch] = useState('')
  const [onlyAI, setOnlyAI] = useState(false)
  const [sortBy, setSortBy] = useState<SortBy>('date')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')
  const [selectedWindow, setSelectedWindow] = useState<RecommendationWindow>(30)

  const user = useAuthStore((s) => s.user)
  const userId = user?.id
  const whitelist = useWhitelistGate(userId)

  const isWhitelisted = whitelist.data === true

  const { data = [], isLoading: loading, error: fetchError } = useQuery({
    queryKey: ['tracking', market],
    queryFn: () => fetchTracking(market),
    enabled: isWhitelisted,
    retry: 1,
  })

  const latestDates = useMemo(() => getLatestRecommendDates(data, RETENTION_DATES), [data])
  const activeDates = useMemo(() => latestDates.slice(0, selectedWindow), [latestDates, selectedWindow])
  const windowRows = useMemo(() => {
    const dateSet = new Set(activeDates)
    return data.filter((row) => dateSet.has(row.recommend_date))
  }, [data, activeDates])
  const visibleData = useMemo(() => dedupeRecommendations(windowRows), [windowRows])

  const filtered = useMemo(() => {
    let result = visibleData
    if (search) {
      const q = search.toLowerCase()
      result = result.filter(
        (r) => String(r.code).includes(q) || (r.name ?? '').toLowerCase().includes(q),
      )
    }
    if (onlyAI) {
      result = result.filter((r) => r.is_ai_recommended)
    }
    return sortRecommendations(result, sortBy, sortOrder)
  }, [visibleData, search, onlyAI, sortBy, sortOrder])

  const stats = useMemo(() => buildSummaryStats(visibleData), [visibleData])
  const latestDate = latestDates[0] ?? null
  const oldestDate = latestDates.at(-1) ?? null
  const activeOldestDate = activeDates.at(-1) ?? null
  const gateView = whitelistGateView(whitelist, <WyckoffLoading />, <TrackingLockedView />)
  if (gateView) return gateView

  return (
    <div className="h-full overflow-auto p-6">
      <TrackingHeader latestDate={latestDate} oldestDate={oldestDate} />
      <MarketTabs market={market} onMarketChange={setMarket} />
      {fetchError ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-4 text-sm text-destructive">
          {fetchError.message}
        </div>
      ) : loading ? (
        <WyckoffLoading />
      ) : (
        <TrackingReadyContent
          activeDates={activeDates}
          activeOldestDate={activeOldestDate}
          filtered={filtered}
          latestDate={latestDate}
          market={market}
          onlyAI={onlyAI}
          search={search}
          selectedWindow={selectedWindow}
          sortBy={sortBy}
          sortOrder={sortOrder}
          stats={stats}
          visibleData={visibleData}
          windowRows={windowRows}
          onOnlyAIChange={setOnlyAI}
          onSearchChange={setSearch}
          onSelectedWindowChange={setSelectedWindow}
          onSortByChange={setSortBy}
          onSortOrderChange={setSortOrder}
        />
      )}
    </div>
  )
}

function TrackingReadyContent(props: TrackingReadyContentProps) {
  const {
    activeDates,
    activeOldestDate,
    filtered,
    latestDate,
    market,
    onlyAI,
    search,
    selectedWindow,
    sortBy,
    sortOrder,
    stats,
    visibleData,
    windowRows,
    onOnlyAIChange,
    onSearchChange,
    onSelectedWindowChange,
    onSortByChange,
    onSortOrderChange,
  } = props
  const handleSort = useCallback((next: SortBy) => {
    if (next === sortBy) { onSortOrderChange(sortOrder === 'desc' ? 'asc' : 'desc'); return }
    onSortByChange(next); onSortOrderChange('desc')
  }, [onSortByChange, onSortOrderChange, sortBy, sortOrder])
  return (
    <>
      <DateWindowFilter activeDateCount={activeDates.length} activeOldestDate={activeOldestDate} latestDate={latestDate} rawCount={windowRows.length} selectedWindow={selectedWindow} onWindowChange={onSelectedWindowChange} />
      {stats && <SummaryCards selectedWindow={selectedWindow} stats={stats} />}
      <WinRatePanel rows={visibleData} />
      <TrackingFilters filteredCount={filtered.length} market={market} onlyAI={onlyAI} search={search} sortBy={sortBy} sortOrder={sortOrder} visibleCount={visibleData.length} onOnlyAIChange={onOnlyAIChange} onSearchChange={onSearchChange} onSortByChange={onSortByChange} onSortOrderChange={onSortOrderChange} />
      <TrackingTable rows={filtered} sortBy={sortBy} sortOrder={sortOrder} onSortChange={handleSort} market={market} />
    </>
  )
}

function MarketTabs({ market, onMarketChange }: { market: MarketTab; onMarketChange: (m: MarketTab) => void }) {
  const { t } = usePreferences()
  const tabs: { key: MarketTab; label: string }[] = [
    { key: 'cn', label: t('tracking.tabCN') },
    { key: 'us', label: t('tracking.tabUS') },
    { key: 'hk', label: t('tracking.tabHK') },
  ]
  return (
    <div className="mb-4 flex gap-1 rounded-lg border border-border p-1 w-fit">
      {tabs.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          onClick={() => onMarketChange(key)}
          className={`rounded-md px-3 py-1 text-sm font-medium transition-colors ${market === key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted'}`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function TrackingLockedView() {
  const { t } = usePreferences()
  return (
    <div className="h-full overflow-auto p-6">
      <div className="mx-auto flex min-h-[calc(100vh-8rem)] max-w-6xl flex-col justify-center gap-6">
        <div className="grid gap-6 lg:grid-cols-[1.35fr_0.65fr]">
          <section className="py-4">
            <div className="inline-flex items-center gap-2 rounded-full border border-sky-500/30 bg-sky-500/10 px-3 py-1 text-xs font-medium text-sky-700 dark:text-sky-300">
              <ShieldCheck className="h-3.5 w-3.5" />
              {t('tracking.locked.eyebrow')}
            </div>
            <h1 className="mt-4 max-w-3xl text-2xl font-semibold tracking-tight text-foreground">
              {t('tracking.locked.title')}
            </h1>
            <p className="mt-3 max-w-3xl text-base leading-7 text-muted-foreground">
              {t('tracking.locked.description')}
            </p>
            <div className="mt-6 grid gap-3 md:grid-cols-3">
              {LOCKED_BENEFITS.map((item) => (
                <TrackingLockedBenefit key={item.titleKey} title={t(item.titleKey)} desc={t(item.descKey)} />
              ))}
            </div>
            <p className="mt-5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm leading-6 text-amber-800 dark:text-amber-200">
              {t('tracking.locked.costNote')}
            </p>
          </section>
          <TrackingLockedAccessCard />
        </div>
      </div>
    </div>
  )
}

function TrackingLockedBenefit({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-4 shadow-sm">
      <CheckCircle2 className="mb-3 h-4 w-4 text-sky-600 dark:text-sky-300" />
      <div className="text-sm font-semibold text-foreground">{title}</div>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{desc}</p>
    </div>
  )
}

function TrackingLockedAccessCard() {
  const { t } = usePreferences()
  return (
    <aside className="rounded-lg border border-border bg-card p-5 shadow-sm">
      <div className="text-base font-semibold text-foreground">{t('tracking.locked.ctaTitle')}</div>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{t('tracking.locked.ctaDesc')}</p>
      <div className="mt-4 rounded-lg border border-border bg-white p-3">
        <img src="/zsxq_qr.jpg" alt={t('tracking.locked.qrAlt')} className="h-auto w-full rounded-md object-contain" />
      </div>
      <div className="mt-4 grid gap-2">
        <a href="/guide#capability-boundary" className="inline-flex items-center justify-center gap-2 rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700">
          {t('tracking.locked.join')}
          <ArrowRight className="h-4 w-4" />
        </a>
        <a href="https://github.com/YoungCan-Wang/WyckoffTradingAgent/blob/main/docs/COST_MODEL.md" target="_blank" rel="noreferrer" className="inline-flex items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted">
          {t('tracking.locked.costLink')}
          <ExternalLink className="h-4 w-4" />
        </a>
      </div>
      <p className="mt-4 text-xs leading-5 text-muted-foreground">{t('tracking.locked.memberHint')}</p>
    </aside>
  )
}

function TrackingHeader({ latestDate, oldestDate }: { latestDate: number | null; oldestDate: number | null }) {
  const { t } = usePreferences()

  return (
    <div className="mb-5">
      <h1 className="text-xl font-semibold">{t('tracking.title')}</h1>
      <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
        {t('tracking.description')}
        {latestDate && oldestDate && (
          <span className="ml-1">
            {t('tracking.range', { oldest: formatDate(oldestDate), latest: formatDate(latestDate) })}
          </span>
        )}
      </p>
    </div>
  )
}

function DateWindowFilter({
  activeDateCount,
  activeOldestDate,
  latestDate,
  rawCount,
  selectedWindow,
  onWindowChange,
}: {
  activeDateCount: number
  activeOldestDate: number | null
  latestDate: number | null
  rawCount: number
  selectedWindow: RecommendationWindow
  onWindowChange: (value: RecommendationWindow) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">{t('tracking.window')}</span>
        <select
          value={selectedWindow}
          onChange={(event) => onWindowChange(Number(event.target.value) as RecommendationWindow)}
          className="rounded-lg border border-border px-2 py-1.5 text-sm"
        >
          {AVG_WINDOWS.map((size) => (
            <option key={size} value={size}>
              {t('tracking.windowOption', { size })}
            </option>
          ))}
        </select>
      </label>
      {latestDate && activeOldestDate && (
        <span className="text-xs text-muted-foreground">
          {t('tracking.currentWindow', {
            oldest: formatDate(activeOldestDate),
            latest: formatDate(latestDate),
            count: activeDateCount,
            rows: rawCount,
          })}
        </span>
      )}
    </div>
  )
}

function SummaryCards({ selectedWindow, stats }: { selectedWindow: RecommendationWindow; stats: SummaryStats }) {
  const { t } = usePreferences()

  return (
    <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-5">
      <StatCard label={t('tracking.coveredStocks')} value={`${stats.count} ${t('common.stocks')}`} />
      <StatCard label={t('tracking.avgChange', { size: selectedWindow })} value={formatPct(stats.avg)} color={financialValueClass(stats.avg)} />
      <StatCard label={t('tracking.bestChange')} value={formatPct(stats.best)} color={financialValueClass(stats.best)} />
      <StatCard label={t('tracking.worstChange')} value={formatPct(stats.worst)} color={financialValueClass(stats.worst)} />
      <StatCard label={t('tracking.totalRecommendations')} value={`${stats.totalRecommendations} ${t('tracking.times')}`} />
    </div>
  )
}

function TrackingFilters({
  filteredCount,
  market,
  onlyAI,
  search,
  sortBy,
  sortOrder,
  visibleCount,
  onOnlyAIChange,
  onSearchChange,
  onSortByChange,
  onSortOrderChange,
}: {
  filteredCount: number
  market: MarketTab
  onlyAI: boolean
  search: string
  sortBy: SortBy
  sortOrder: SortOrder
  visibleCount: number
  onOnlyAIChange: (value: boolean) => void
  onSearchChange: (value: string) => void
  onSortByChange: (value: SortBy) => void
  onSortOrderChange: (value: SortOrder) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="mb-4 flex items-center gap-3">
      <input
        type="text"
        value={search}
        onChange={(event) => onSearchChange(event.target.value)}
        placeholder={t('tracking.searchPlaceholder')}
        className="rounded-lg border border-border px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring/20"
      />
      <label className="flex items-center gap-1.5 text-sm">
        <input
          type="checkbox"
          checked={onlyAI}
          onChange={(event) => onOnlyAIChange(event.target.checked)}
          className="rounded"
        />
        {t('tracking.onlyAI')}
      </label>
      <TrackingSortControls
        market={market}
        sortBy={sortBy}
        sortOrder={sortOrder}
        onSortByChange={onSortByChange}
        onSortOrderChange={onSortOrderChange}
      />
      <span className="text-xs text-muted-foreground">
        {filteredCount} / {visibleCount} {t('common.stocks')}
      </span>
    </div>
  )
}

function TrackingSortControls({
  market,
  sortBy,
  sortOrder,
  onSortByChange,
  onSortOrderChange,
}: {
  market: MarketTab
  sortBy: SortBy
  sortOrder: SortOrder
  onSortByChange: (value: SortBy) => void
  onSortOrderChange: (value: SortOrder) => void
}) {
  const { t } = usePreferences()
  return (
    <>
      <select value={sortBy} onChange={(event) => onSortByChange(event.target.value as SortBy)} className="rounded-lg border border-border px-2 py-1.5 text-sm">
        <option value="date">{t('tracking.sortDate')}</option>
        <option value="change">{t('tracking.sortChange')}</option>
        {market === 'us' && <option value="mfe">{t('tracking.sortMfe')}</option>}
        {market === 'us' && <option value="mae">{t('tracking.sortMae')}</option>}
        <option value="score">{t('tracking.sortScore')}</option>
        <option value="count">{t('tracking.sortRecommendCount')}</option>
      </select>
      <select value={sortOrder} onChange={(event) => onSortOrderChange(event.target.value as SortOrder)} className="rounded-lg border border-border px-2 py-1.5 text-sm">
        <option value="desc">{t('tracking.sortDesc')}</option>
        <option value="asc">{t('tracking.sortAsc')}</option>
      </select>
    </>
  )
}

function TrackingTable({
  rows,
  sortBy,
  sortOrder,
  onSortChange,
  market = 'cn',
}: {
  rows: Recommendation[]
  sortBy: SortBy
  market?: MarketTab
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()

  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="overflow-x-auto">
        <table className="min-w-[1240px] w-full text-sm">
          <TrackingTableHead market={market} sortBy={sortBy} sortOrder={sortOrder} onSortChange={onSortChange} />
          <tbody style={{ contentVisibility: 'auto', containIntrinsicSize: '0 40000px' }}>
            {rows.length === 0 ? (
              <tr className="border-t border-border">
                <td colSpan={trackingColumnCount(market)} className="px-3 py-8 text-center text-muted-foreground">
                  {t('tracking.empty')}
                </td>
              </tr>
            ) : (
              rows.map((row) => <TrackingRow key={`${row.source_type || 'tracking'}-${row.code}-${row.recommend_date}`} row={row} market={market} />)
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function TrackingTableHead({
  market,
  sortBy,
  sortOrder,
  onSortChange,
}: {
  market: MarketTab
  sortBy: SortBy
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()
  return (
    <thead className="sticky top-0 bg-muted/80 backdrop-blur">
      <tr>
        <th className="px-3 py-2 text-left font-medium">{t('common.code')}</th>
        <th className="px-3 py-2 text-left font-medium">{t('common.name')}</th>
        <SortableHeader variant="compact" align="right" active={sortBy === 'date'} label={t('tracking.recommendDate')} order={sortOrder} onClick={() => onSortChange('date')} />
        <SortableHeader variant="compact" align="right" active={sortBy === 'count'} label={t('tracking.recommendCount')} order={sortOrder} onClick={() => onSortChange('count')} />
        <th className="px-3 py-2 text-right font-medium">{t('tracking.initialPrice')}</th>
        <th className="px-3 py-2 text-right font-medium">{t('tracking.currentPrice')}</th>
        <SortableHeader variant="compact" align="right" active={sortBy === 'change'} label={t('tracking.changePct')} order={sortOrder} onClick={() => onSortChange('change')} />
        <SortableHeader variant="compact" align="right" active={sortBy === 'score'} label={t('tracking.score')} order={sortOrder} onClick={() => onSortChange('score')} />
        <th className="px-3 py-2 text-left font-medium">入选路径</th>
        {market === 'us' && <UsPerformanceHeaders sortBy={sortBy} sortOrder={sortOrder} onSortChange={onSortChange} />}
        <th className="px-3 py-2 text-center font-medium">{t('tracking.springboard')}</th>
        <th className="px-3 py-2 text-center font-medium">{t('tracking.actionStatus')}</th>
      </tr>
    </thead>
  )
}

function UsPerformanceHeaders({
  sortBy,
  sortOrder,
  onSortChange,
}: {
  sortBy: SortBy
  sortOrder: SortOrder
  onSortChange: (sortBy: SortBy) => void
}) {
  const { t } = usePreferences()
  return (
    <>
      <SortableHeader variant="compact" align="right" active={sortBy === 'mfe'} label={t('tracking.mfePct')} order={sortOrder} onClick={() => onSortChange('mfe')} />
      <SortableHeader variant="compact" align="right" active={sortBy === 'mae'} label={t('tracking.maePct')} order={sortOrder} onClick={() => onSortChange('mae')} />
      <th className="px-3 py-2 text-right font-medium">{t('tracking.rangeAmpPct')}</th>
    </>
  )
}

function TrackingRow({ row, market = 'cn' }: { row: Recommendation; market?: MarketTab }) {
  const { t } = usePreferences()
  const vetoed = row.rag_vetoed
  const rowCls = vetoed ? 'border-t border-border hover:bg-muted/20 opacity-60 line-through' : 'border-t border-border hover:bg-muted/20'
  const codeDisplay = market === 'cn' ? normalizeCode(row.code) : String(row.code)
  const scoreKind = trackingScoreKind(row)
  return (
    <tr className={rowCls}>
      <td className="px-3 py-2 font-mono">
        {codeDisplay}
        {vetoed && <span className="ml-1 inline-block h-2 w-2 rounded-full bg-red-500" title="RAG veto" />}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <span>{row.name || '-'}</span>
          <SignalPendingBadge row={row} />
        </div>
      </td>
      <td className="px-3 py-2 text-right text-muted-foreground">{formatDate(row.recommend_date)}</td>
      <td className="px-3 py-2 text-right font-medium">{recommendationCount(row.recommend_count)}</td>
      <td className="px-3 py-2 text-right">{row.initial_price?.toFixed(2) || '-'}</td>
      <td className="px-3 py-2 text-right">{row.current_price?.toFixed(2) || '-'}</td>
      <td className={`px-3 py-2 text-right font-medium ${financialValueClass(row.change_pct)}`}>{formatPct(row.change_pct)}</td>
      <td className="px-3 py-2 text-right">
        <div className="flex flex-col items-end gap-0.5">
          <span>{formatScore(row.funnel_score)}</span>
          {scoreKind && (
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] leading-none text-muted-foreground">
              {scoreKind === 'priority' ? t('tracking.scorePriority') : t('tracking.scoreRaw')}
            </span>
          )}
        </div>
      </td>
      <td className="px-3 py-2">
        <CandidateLaneBadge row={row} />
      </td>
      {market === 'us' && <UsPerformanceCells row={row} />}
      <td className="px-3 py-2 text-center">
        <SpringboardBadge row={row} />
      </td>
      <td className="px-3 py-2 text-center">
        <ActionStatusBadge row={row} />
      </td>
    </tr>
  )
}

function ActionStatusBadge({ row }: { row: Recommendation }) {
  const action = resolveActionStatus(row)
  if (!action.label) {
    return row.is_ai_recommended ? <span className="inline-block h-2 w-2 rounded-full bg-indigo-500" /> : <span className="text-muted-foreground">-</span>
  }
  return (
    <span className={`inline-flex max-w-[7.5rem] items-center justify-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${action.cls}`} title={action.title}>
      {row.is_ai_recommended && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-500" />}
      <span className="truncate">{action.label}</span>
    </span>
  )
}

function SignalPendingBadge({ row }: { row: Recommendation }) {
  const { t } = usePreferences()
  if (row.source_type !== 'signal_pending') return null
  const confirmed = cleanText(row.signal_status) === 'confirmed'
  const cls = confirmed
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
    : 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  return (
    <span className={`whitespace-nowrap rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>
      {confirmed ? t('tracking.confirmedSignal') : t('tracking.pendingSignal')}
    </span>
  )
}

function SpringboardBadge({ row }: { row: Recommendation }) {
  const combo = springboardCombo(row)
  if (combo === '-') return <span className="text-muted-foreground">-</span>
  const active = row.is_ai_recommended && combo !== 'none'
  const cls = active
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
    : 'border-border bg-muted text-muted-foreground'
  return <span className={`inline-flex min-w-[3.5rem] justify-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}>{combo}</span>
}

function CandidateLaneBadge({ row }: { row: Recommendation }) {
  const lane = labelCandidateTerm(cleanText(row.candidate_lane || row.signal_key))
  const entry = labelCandidateTerm(cleanText(row.entry_type || row.candidate_timing))
  const status = labelCandidateTerm(cleanText(row.candidate_status))
  if (!lane && !entry && !status) return <span className="text-muted-foreground">-</span>
  const score = isFiniteNumber(row.mainline_score) ? `${Math.round((row.mainline_score ?? 0) * 100)}` : ''
  return (
    <div className="flex min-w-[8rem] max-w-[14rem] flex-col gap-1">
      <span className="inline-flex w-fit rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs font-medium text-sky-700 dark:text-sky-300">
        {lane || 'candidate'}{score ? ` ${score}` : ''}
      </span>
      {(entry || status) && (
        <span className="truncate text-xs text-muted-foreground" title={[entry, status].filter(Boolean).join(' | ')}>
          {[entry, status].filter(Boolean).join(' | ')}
        </span>
      )}
    </div>
  )
}

function springboardCombo(row: Recommendation): string {
  const raw = (row.springboard_combo || row.springboard_grade || '').trim()
  if (raw) return raw
  const parts = [
    row.springboard_a ? 'A' : '',
    row.springboard_b ? 'B' : '',
    row.springboard_c ? 'C' : '',
  ].filter(Boolean)
  if (parts.length > 0) return parts.join('+')
  return row.springboard_scored ? 'none' : '-'
}

function trackingColumnCount(market: MarketTab): number {
  if (market === 'us') return 13
  return 10
}

function cleanText(value: string | null | undefined): string {
  const text = String(value || '').trim()
  return text && text !== 'none' ? text : ''
}

function resolveActionStatus(row: Recommendation): { label: string; cls: string; title: string } {
  const status = cleanText(row.action_status || row.candidate_status)
  const label = cleanText(row.action_label) || actionStatusLabel(status, row)
  const level = cleanText(row.action_level) || actionStatusLevel(status, row)
  const direct = isTruthy(row.direct_buy_allowed)
  const cls = direct
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
    : actionStatusClass(level)
  const title = [
    label,
    status ? `status=${status}` : '',
    direct ? '允许直接买入' : '不等于直接买入',
  ].filter(Boolean).join(' · ')
  return { label, cls, title }
}

function actionStatusLabel(status: string, row: Recommendation): string {
  const labels: Record<string, string> = {
    ready_for_ai_review: '可进入AI复核',
    repair_review_only: '只做修复复核',
    confirmation_required: '等待确认',
    watch_only: '观察池',
    priority_watch: '重点观察',
    trigger_watch: '触发观察',
    caution_watch: '警戒观察',
    watch: '观察',
    avoid: '回避',
    blocked_by_market_gate: '风险闸门关闭',
    blocked_by_data_quality: '数据质量未过关',
    blocked_by_policy_guard: '策略护栏阻断',
    blocked_by_quality_gate: '质量门槛阻断',
    blocked_by_watch_only: '仅观察阻断',
    pending: '待确认',
    confirmed: '信号确认',
  }
  if (labels[status]) return labels[status]
  if (status.startsWith('blocked_')) return '阻断'
  if (row.is_ai_recommended) return 'AI入选'
  return status
}

function actionStatusLevel(status: string, row: Recommendation): string {
  if (status.startsWith('blocked_') || status === 'avoid') return 'blocked'
  if (status === 'ready_for_ai_review' || row.is_ai_recommended) return 'ai_review'
  if (status === 'confirmation_required' || status === 'pending' || status === 'confirmed') return 'confirmation'
  if (status === 'repair_review_only') return 'review_only'
  if (status === 'watch_only' || status.endsWith('_watch') || status === 'watch') return 'watch'
  return ''
}

function actionStatusClass(level: string): string {
  if (level === 'blocked') return 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300'
  if (level === 'ai_review') return 'border-indigo-500/30 bg-indigo-500/10 text-indigo-700 dark:text-indigo-300'
  if (level === 'confirmation') return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  if (level === 'review_only') return 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300'
  return 'border-border bg-muted text-muted-foreground'
}

function signalActionStatus(status: string | null | undefined): string {
  return cleanText(status) === 'confirmed' ? 'confirmation_required' : 'watch_only'
}

function isTruthy(value: boolean | string | number | null | undefined): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value === 1
  if (typeof value === 'string') return ['1', 'true', 'yes', 'y'].includes(value.trim().toLowerCase())
  return false
}

function trackingScoreKind(row: Recommendation): 'priority' | 'raw' | null {
  if (!isFiniteNumber(row.funnel_score)) return null
  const reason = row.recommend_reason ?? ''
  if (row.funnel_score >= 20) return 'priority'
  if (row.funnel_score >= 10 && (reason.includes('点火破局') || reason.includes('吸筹通道') || reason.includes('趋势延续'))) {
    return 'priority'
  }
  return 'raw'
}

function UsPerformanceCells({ row }: { row: Recommendation }) {
  return (
    <>
      <td className={`px-3 py-2 text-right font-medium ${financialValueClass(row.mfe_pct ?? null)}`}>
        {formatPct(row.mfe_pct ?? null)}
      </td>
      <td className={`px-3 py-2 text-right font-medium ${financialValueClass(row.mae_pct ?? null)}`}>
        {formatPct(row.mae_pct ?? null)}
      </td>
      <td className="px-3 py-2 text-right text-muted-foreground">{formatPct(row.range_amp_pct ?? null)}</td>
    </>
  )
}

function WinRatePanel({ rows }: { rows: Recommendation[] }) {
  const { t } = usePreferences()
  const values = useMemo(() => rows.map((r) => r.change_pct).filter(isFiniteNumber), [rows])
  if (values.length < 3) return null
  const wins = values.filter((v) => v > 0)
  const losses = values.filter((v) => v <= 0)
  const winRate = (wins.length / values.length) * 100
  const avgWin = wins.length > 0 ? wins.reduce((a, b) => a + b, 0) / wins.length : 0
  const avgLoss = losses.length > 0 ? losses.reduce((a, b) => a + b, 0) / losses.length : 0
  return (
    <div className="mb-5 space-y-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label={t('tracking.winRate')} value={`${winRate.toFixed(1)}%`} color={winRate >= 50 ? 'text-up' : 'text-down'} />
        <StatCard label={t('tracking.avgWin')} value={formatPct(avgWin)} color="text-up" />
        <StatCard label={t('tracking.avgLoss')} value={formatPct(avgLoss)} color="text-down" />
        <StatCard label={t('tracking.profitFactor')} value={avgLoss !== 0 ? Math.abs(avgWin / avgLoss).toFixed(2) : '--'} />
      </div>
      <ReturnHistogram values={values} />
    </div>
  )
}

function ReturnHistogram({ values }: { values: number[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const buckets = useMemo(() => buildReturnBuckets(values), [values])
  useEffect(() => {
    if (!containerRef.current || buckets.length === 0) return
    const isDark = document.documentElement.classList.contains('dark')
    const chart = createChart(containerRef.current, {
      height: 140,
      layout: { background: { color: isDark ? '#0f172a' : '#ffffff' }, textColor: isDark ? '#94a3b8' : '#6b7194', fontSize: 10 },
      grid: { vertLines: { visible: false }, horzLines: { color: isDark ? '#202938' : '#eef1f6' } },
      rightPriceScale: { visible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
    })
    const series = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false })
    series.setData(
      buckets.map((b, i) => ({ time: (2020 * 10000 + 101 + i) as unknown as Time, value: b.count, color: b.midPct >= 0 ? '#ef4444a0' : '#10b981a0' })),
    )
    chart.timeScale().fitContent()
    const stopResize = watchChartResize(containerRef.current, chart)
    return () => { stopResize(); chart.remove() }
  }, [buckets])
  return (
    <div>
      <div className="mb-1 text-[11px] text-muted-foreground">收益分布</div>
      <div ref={containerRef} className="w-full overflow-hidden rounded-lg border border-border" />
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
        {buckets.length > 0 && <span>{buckets[0]!.midPct.toFixed(0)}%</span>}
        <span>0%</span>
        {buckets.length > 0 && <span>{buckets[buckets.length - 1]!.midPct.toFixed(0)}%</span>}
      </div>
    </div>
  )
}

function buildReturnBuckets(values: number[]): { midPct: number; count: number }[] {
  if (values.length === 0) return []
  const min = Math.min(...values), max = Math.max(...values)
  const step = Math.max((max - min) / 15, 1)
  const buckets: { midPct: number; count: number }[] = []
  for (let lo = Math.floor(min / step) * step; lo < max + step; lo += step) {
    buckets.push({ midPct: lo + step / 2, count: values.filter((v) => v >= lo && v < lo + step).length })
  }
  return buckets
}

function StatCard({ label, value, color, hint }: { label: string; value: string; color?: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${color || ''}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

function getLatestRecommendDates(rows: Recommendation[], limit: number): number[] {
  const dates = rows
    .map((row) => row.recommend_date)
    .filter((date) => Number.isFinite(date) && date > 0)
  return [...new Set(dates)].sort((a, b) => b - a).slice(0, limit)
}

function dedupeRecommendations(rows: Recommendation[]): Recommendation[] {
  return dedupeTrackingRows(rows)
}

function buildSummaryStats(rows: Recommendation[]): SummaryStats | null {
  if (rows.length === 0) return null
  const totalRecommendations = rows.reduce((total, row) => total + recommendationCount(row.recommend_count), 0)
  const activeRows = rows.filter((row) => !row.rag_vetoed)
  const values = activeRows.map((row) => row.change_pct).filter(isFiniteNumber)
  if (values.length === 0) {
    return {
      count: rows.length,
      avg: null,
      best: null,
      worst: null,
      totalRecommendations,
    }
  }
  const sum = values.reduce((total, value) => total + value, 0)
  return {
    count: rows.length,
    avg: sum / values.length,
    best: Math.max(...values),
    worst: Math.min(...values),
    totalRecommendations,
  }
}

function recommendationCount(value: number | null | undefined): number {
  return isFiniteNumber(value) && value > 0 ? Math.trunc(value) : 1
}

function nullableNumber(value: number | null | undefined): number | null {
  return isFiniteNumber(value) ? value : null
}

function signalDateNumber(value: string | null | undefined): number {
  const digits = String(value || '').replaceAll('-', '')
  return /^\d{8}$/.test(digits) ? Number(digits) : 0
}

function sortRecommendations(rows: Recommendation[], sortBy: SortBy, sortOrder: SortOrder): Recommendation[] {
  const direction = sortOrder === 'desc' ? 1 : -1
  return [...rows].sort((a, b) => {
    if (sortBy === 'date') return nullableNumberCompare(a.recommend_date, b.recommend_date, direction)
    if (sortBy === 'change') return nullableNumberCompare(a.change_pct, b.change_pct, direction)
    if (sortBy === 'mfe') return nullableNumberCompare(a.mfe_pct, b.mfe_pct, direction)
    if (sortBy === 'mae') return nullableNumberCompare(a.mae_pct, b.mae_pct, direction)
    if (sortBy === 'count') return nullableNumberCompare(recommendationCount(a.recommend_count), recommendationCount(b.recommend_count), direction)
    return nullableNumberCompare(a.funnel_score, b.funnel_score, direction)
  })
}

function nullableNumberCompare(a: number | null | undefined, b: number | null | undefined, direction: number): number {
  if (isFiniteNumber(a) && isFiniteNumber(b)) return (b - a) * direction
  if (isFiniteNumber(a)) return -1
  if (isFiniteNumber(b)) return 1
  return 0
}

function formatPct(value: number | null): string {
  return formatSignedPercent(value, 2, '-')
}

function formatScore(value: number | null): string {
  if (!isFiniteNumber(value)) return '-'
  return value >= 10 ? value.toFixed(1) : value.toFixed(2)
}

function formatDate(d: number): string {
  const s = String(d)
  if (s.length !== 8) return s
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`
}
