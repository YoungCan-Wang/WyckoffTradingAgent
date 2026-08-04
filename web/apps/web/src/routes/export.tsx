import { useMemo, useState, type ReactNode } from 'react'
import { Download, FileSpreadsheet, Loader2, Package } from 'lucide-react'
import { StockSearchBox, matchesSelectedQuery, useStockSearch, type StockSearchController } from '@/components/stock-search-box'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'
import { usePreferences } from '@/lib/preferences'
import { resolveStockQuery } from '@/lib/market-search'
import {
  arrayToCSV,
  buildEnhancedRows,
  buildKlineParams,
  createZipBlob,
  downloadBlob,
  exportSymbolFromStock,
  normalizeExportSymbol,
  parseExportSymbols,
  parseTickFlowToRows,
  type ExportAdjust,
  type ExportDataset,
  type ExportRow,
} from '@/lib/export-data'

type ExportMode = 'single' | 'batch'
type PreviewMode = 'enhanced' | 'raw'
type Translate = ReturnType<typeof usePreferences>['t']

interface BatchResult {
  symbol: string
  status: 'ok' | 'failed'
  rows: number
  error: string
}

interface ExportFetchParams {
  days: number
  endOffset: number
  adjust: ExportAdjust
}

async function getTickFlowKey(userId: string): Promise<string | null> {
  const { data } = await supabase
    .from('user_settings')
    .select('tickflow_api_key')
    .eq('user_id', userId)
    .single()
  return data?.tickflow_api_key || null
}

export function ExportPage() {
  const user = useAuthStore((s) => s.user)
  const { t } = usePreferences()
  const state = useExportState(user?.id)

  return (
    <div className="flex h-full flex-col p-6">
      <h1 className="mb-5 text-xl font-semibold">{t('export.title')}</h1>
      <ExportControlPanel {...state.control} />
      <ExportError error={state.error} />
      {state.batchResults.length > 0 && <BatchStatus rows={state.batchResults} okText={t('export.ok')} failedText={t('export.failedStatus')} rowsText={t('common.rows')} />}
      {state.activeDataset ? (
        <DatasetPreviewPanel {...state.preview} />
      ) : (
        !state.loading && <EmptyState title={t('export.emptyTitle')} subtitle={t('export.emptySubtitle')} />
      )}
    </div>
  )
}

function useExportState(userId: string | undefined) {
  const { t } = usePreferences()
  const [mode, setMode] = useState<ExportMode>('single')
  const search = useStockSearch('export')
  const [batchText, setBatchText] = useState('')
  const [days, setDays] = useState(320)
  const [endOffset, setEndOffset] = useState(1)
  const [adjust, setAdjust] = useState<ExportAdjust>('qfq')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [datasets, setDatasets] = useState<ExportDataset[]>([])
  const [batchResults, setBatchResults] = useState<BatchResult[]>([])
  const [activeIndex, setActiveIndex] = useState(0)
  const [previewMode, setPreviewMode] = useState<PreviewMode>('enhanced')
  const [columnFilter, setColumnFilter] = useState('')
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])

  const activeDataset = datasets[activeIndex] || null
  const activeRows = useMemo(
    () => activeDataset ? (previewMode === 'enhanced' ? activeDataset.enhancedRows : activeDataset.rawRows) : [],
    [activeDataset, previewMode],
  )
  const columns = useMemo(() => Object.keys(activeRows[0] || {}), [activeRows])
  const activeSelectedColumns = useMemo(() => selectedColumns.filter((column) => columns.includes(column)), [columns, selectedColumns])
  const columnSet = useMemo(() => new Set(activeSelectedColumns.length ? activeSelectedColumns : columns), [columns, activeSelectedColumns])
  const visibleColumns = useMemo(() => filterColumns(columns, columnFilter), [columns, columnFilter])
  const previewRows = useMemo(() => selectColumns(activeRows.slice(0, 10), columnsFromSet(columnSet, columns)), [activeRows, columnSet, columns])

  async function handleExport() {
    const apiKey = userId ? await getTickFlowKey(userId) : null
    if (!apiKey) {
      setError(t('export.configureTickflow'))
      return
    }
    const validation = await resolveExportSymbols(mode, search, batchText, t)
    if (validation.error) {
      setError(validation.error)
      return
    }
    if (mode === 'single' && validation.symbols[0]) {
      search.setSymbol(validation.symbols[0])
      search.setSearchOpen(false)
    }
    resetExportOutput(setError, setDatasets, setBatchResults, setActiveIndex, setSelectedColumns, setColumnFilter)
    setLoading(true)
    const result = await collectExportDatasets(apiKey, validation.symbols, { days, endOffset, adjust })
    setDatasets(result.datasets)
    setBatchResults(result.batchResults)
    setError(result.datasets.length ? '' : t('export.failed'))
    setLoading(false)
  }

  function downloadCurrent(kind: PreviewMode) {
    if (!activeDataset) return
    const rows = kind === 'enhanced' ? activeDataset.enhancedRows : activeDataset.rawRows
    downloadCsv(rows, `${activeDataset.fileStem}_${kind}.csv`)
  }

  function downloadSelected() {
    if (!activeDataset || activeRows.length === 0) return
    const selected = columnsFromSet(columnSet, columns)
    downloadCsv(selectColumns(activeRows, selected), `${activeDataset.fileStem}_${previewMode}_selected.csv`)
  }

  function downloadZip() {
    if (!datasets.length) return
    downloadBlob(createZipBlob(zipFiles(datasets)), `wyckoff_export_${formatStamp()}.zip`)
  }

  return {
    activeDataset,
    batchResults,
    error,
    loading,
    control: {
      mode,
      setMode,
      search,
      batchText,
      setBatchText,
      days,
      setDays,
      endOffset,
      setEndOffset,
      adjust,
      setAdjust,
      loading,
      onExport: handleExport,
      onClearError: () => setError(''),
    },
    preview: {
      datasets,
      activeIndex,
      setActiveIndex,
      previewMode,
      setPreviewMode,
      columnFilter,
      setColumnFilter,
      visibleColumns,
      columnSet,
      setSelectedColumns,
      columns,
      previewRows,
      downloadCurrent,
      downloadSelected,
      downloadZip,
    },
  }
}

function ExportControlPanel(props: {
  mode: ExportMode
  setMode: (mode: ExportMode) => void
  search: StockSearchController
  batchText: string
  setBatchText: (value: string) => void
  days: number
  setDays: (value: number) => void
  endOffset: number
  setEndOffset: (value: number) => void
  adjust: ExportAdjust
  setAdjust: (value: ExportAdjust) => void
  loading: boolean
  onExport: () => void
  onClearError: () => void
}) {
  const { t } = usePreferences()
  const disabled = props.loading || (props.mode === 'single' ? !props.search.symbol.trim() : !props.batchText.trim())
  return (
    <section className="mb-5 rounded-lg border border-border p-4">
      <div className="mb-4 flex flex-wrap gap-2">
        <ModeButton active={props.mode === 'single'} onClick={() => props.setMode('single')}>{t('export.singleMode')}</ModeButton>
        <ModeButton active={props.mode === 'batch'} onClick={() => props.setMode('batch')}>{t('export.batchMode')}</ModeButton>
      </div>
      <div className="grid gap-3 lg:grid-cols-[minmax(240px,1fr)_120px_120px_150px_auto] lg:items-end">
        {props.mode === 'single' ? (
          <StockSearchBox
            search={props.search}
            onSubmit={props.onExport}
            onClearError={props.onClearError}
            placeholder={t('analysis.searchPlaceholder')}
            listboxId="export-stock-search"
          />
        ) : (
          <TextArea label={t('export.batchSymbols')} value={props.batchText} onChange={props.setBatchText} placeholder="601318; 000001; 510300; AAPL.US; 00700.HK" />
        )}
        <NumberField label={t('export.days')} value={props.days} min={10} max={700} onChange={props.setDays} />
        <NumberField label={t('export.endOffset')} value={props.endOffset} min={0} max={30} onChange={props.setEndOffset} />
        <AdjustSelect value={props.adjust} onChange={props.setAdjust} />
        <button onClick={props.onExport} disabled={disabled} className="flex h-10 items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground disabled:opacity-50">
          {props.loading ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
          {props.loading ? t('export.fetching') : t('export.fetch')}
        </button>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">{t('export.batchHint')}</p>
    </section>
  )
}

async function resolveExportSymbols(
  mode: ExportMode,
  search: StockSearchController,
  batchText: string,
  t: Translate,
): Promise<{ symbols: string[]; error: string }> {
  if (mode === 'batch') {
    const symbols = parseExportSymbols(batchText)
    if (symbols.length === 0) return { symbols, error: t('export.batchEmpty') }
    if (symbols.length > 6) return { symbols, error: t('export.batchTooMany', { count: symbols.length }) }
    return { symbols, error: '' }
  }
  const symbol = await resolveSingleExportSymbol(search.symbol, search.selectedStock)
  if (!symbol) return { symbols: [], error: t('export.invalidSymbol') }
  return { symbols: [symbol], error: '' }
}

async function resolveSingleExportSymbol(
  rawInput: string,
  selected: StockSearchController['selectedStock'],
): Promise<string> {
  const raw = rawInput.trim()
  if (!raw) return ''
  if (matchesSelectedQuery(raw, selected, 'export')) {
    return exportSymbolFromStock(selected)
  }
  const direct = normalizeExportSymbol(raw)
  if (direct) return direct
  return exportSymbolFromStock(await resolveStockQuery(raw))
}

async function collectExportDatasets(apiKey: string, symbols: string[], params: ExportFetchParams) {
  const batchResults: BatchResult[] = []
  const datasets: ExportDataset[] = []
  for (const item of symbols) {
    try {
      const dataset = await fetchDataset(apiKey, item, params.days, params.endOffset, params.adjust)
      datasets.push(dataset)
      batchResults.push({ symbol: item, status: 'ok', rows: dataset.rawRows.length, error: '' })
    } catch (err) {
      batchResults.push({ symbol: item, status: 'failed', rows: 0, error: errorMessage(err) })
    }
  }
  return { batchResults, datasets }
}

function resetExportOutput(
  setError: (value: string) => void,
  setDatasets: (value: ExportDataset[]) => void,
  setBatchResults: (value: BatchResult[]) => void,
  setActiveIndex: (value: number) => void,
  setSelectedColumns: (value: string[]) => void,
  setColumnFilter: (value: string) => void,
) {
  setError('')
  setDatasets([])
  setBatchResults([])
  setActiveIndex(0)
  setSelectedColumns([])
  setColumnFilter('')
}

function AdjustSelect({ value, onChange }: { value: ExportAdjust; onChange: (value: ExportAdjust) => void }) {
  const { t } = usePreferences()
  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium">{t('export.adjust')}</label>
      <select value={value} onChange={(e) => onChange(e.target.value as ExportAdjust)} className="h-10 w-full rounded-lg border border-border bg-background px-3 text-sm">
        <option value="qfq">{t('export.qfq')}</option>
        <option value="hfq">{t('export.hfq')}</option>
        <option value="">{t('export.noneAdjust')}</option>
      </select>
    </div>
  )
}

function ExportError({ error }: { error: string }) {
  if (!error) return null
  return <div className="mb-4 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-200">{error}</div>
}

function DatasetPreviewPanel(props: {
  datasets: ExportDataset[]
  activeIndex: number
  setActiveIndex: (index: number) => void
  previewMode: PreviewMode
  setPreviewMode: (mode: PreviewMode) => void
  columnFilter: string
  setColumnFilter: (filter: string) => void
  visibleColumns: string[]
  columnSet: Set<string>
  setSelectedColumns: (columns: string[]) => void
  columns: string[]
  previewRows: ExportRow[]
  downloadCurrent: (kind: PreviewMode) => void
  downloadSelected: () => void
  downloadZip: () => void
}) {
  return (
    <section className="min-h-0 flex-1 overflow-hidden rounded-lg border border-border">
      <PreviewToolbar {...props} />
      <ColumnPicker {...props} />
      <PreviewTable rows={props.previewRows} />
    </section>
  )
}

function PreviewToolbar(props: {
  datasets: ExportDataset[]
  activeIndex: number
  setActiveIndex: (index: number) => void
  previewMode: PreviewMode
  setPreviewMode: (mode: PreviewMode) => void
  downloadCurrent: (kind: PreviewMode) => void
  downloadSelected: () => void
  downloadZip: () => void
}) {
  const { t } = usePreferences()
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <DatasetSelect datasets={props.datasets} activeIndex={props.activeIndex} onChange={props.setActiveIndex} label={t('export.dataset')} />
        <ModeButton active={props.previewMode === 'enhanced'} onClick={() => props.setPreviewMode('enhanced')}>{t('export.enhancedView')}</ModeButton>
        <ModeButton active={props.previewMode === 'raw'} onClick={() => props.setPreviewMode('raw')}>{t('export.rawView')}</ModeButton>
      </div>
      <div className="flex flex-wrap gap-2">
        <DownloadButton onClick={() => props.downloadCurrent('enhanced')} icon={<FileSpreadsheet size={15} />}>{t('export.enhancedCsv')}</DownloadButton>
        <DownloadButton onClick={() => props.downloadCurrent('raw')} icon={<FileSpreadsheet size={15} />}>{t('export.rawCsv')}</DownloadButton>
        <DownloadButton onClick={props.downloadSelected} icon={<Download size={15} />}>{t('export.selectedCsv')}</DownloadButton>
        <DownloadButton onClick={props.downloadZip} icon={<Package size={15} />}>{t('export.zip')}</DownloadButton>
      </div>
    </div>
  )
}

function ColumnPicker(props: {
  columnFilter: string
  setColumnFilter: (filter: string) => void
  visibleColumns: string[]
  columnSet: Set<string>
  setSelectedColumns: (columns: string[]) => void
  columns: string[]
}) {
  const { t } = usePreferences()
  return (
    <div className="border-b border-border p-3">
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <input value={props.columnFilter} onChange={(e) => props.setColumnFilter(e.target.value)} placeholder={t('export.columnFilter')} className="h-9 w-56 rounded-lg border border-border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring/20" />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={props.visibleColumns.every((c) => props.columnSet.has(c))} onChange={(e) => props.setSelectedColumns(toggleColumns(props.columnSet, props.columns, props.visibleColumns, e.target.checked))} />
          {t('export.selectAll')}
        </label>
        <span className="text-xs text-muted-foreground">{t('export.selectedCount', { count: props.columnSet.size })}</span>
      </div>
      <div className="flex max-h-20 flex-wrap gap-2 overflow-auto">
        {props.visibleColumns.map((column) => (
          <label key={column} className="flex items-center gap-1.5 rounded border border-border px-2 py-1 text-xs">
            <input type="checkbox" checked={props.columnSet.has(column)} onChange={(e) => props.setSelectedColumns(toggleColumns(props.columnSet, props.columns, [column], e.target.checked))} />
            {column}
          </label>
        ))}
      </div>
    </div>
  )
}

async function fetchDataset(apiKey: string, symbol: string, days: number, endOffset: number, adjust: ExportAdjust): Promise<ExportDataset> {
  const params = buildKlineParams(symbol, days, endOffset, adjust)
  const resp = await fetch(`/api/llm-proxy/v1/klines?${params}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (!resp.ok) throw new Error(`TickFlow ${resp.status}: ${(await resp.text()).slice(0, 180)}`)
  const rawRows = parseTickFlowToRows(await resp.json())
  if (!rawRows.length) throw new Error('no data')
  return { symbol, fileStem: safeStem(symbol), rawRows, enhancedRows: buildEnhancedRows(rawRows) }
}

function downloadCsv(rows: ExportRow[], fileName: string) {
  downloadBlob(new Blob(['\uFEFF' + arrayToCSV(rows)], { type: 'text/csv;charset=utf-8;' }), fileName)
}

function zipFiles(datasets: ExportDataset[]) {
  return datasets.flatMap((dataset) => [
    { name: `${dataset.fileStem}_enhanced.csv`, content: '\uFEFF' + arrayToCSV(dataset.enhancedRows) },
    { name: `${dataset.fileStem}_raw.csv`, content: '\uFEFF' + arrayToCSV(dataset.rawRows) },
  ])
}

function selectColumns(rows: ExportRow[], columns: string[]): ExportRow[] {
  return rows.map((row) => Object.fromEntries(columns.map((column) => [column, row[column] ?? ''])))
}

function toggleColumns(current: Set<string>, allColumns: string[], target: string[], checked: boolean): string[] {
  const next = new Set(current.size ? current : allColumns)
  for (const column of target) {
    if (checked) next.add(column)
    else next.delete(column)
  }
  return allColumns.filter((column) => next.has(column))
}

function columnsFromSet(set: Set<string>, fallback: string[]): string[] {
  const selected = fallback.filter((column) => set.has(column))
  return selected.length ? selected : fallback
}

function filterColumns(columns: string[], filter: string): string[] {
  const q = filter.trim().toLowerCase()
  return q ? columns.filter((column) => column.toLowerCase().includes(q)) : columns
}

function safeStem(symbol: string): string {
  return symbol.replace(/[^0-9A-Z.-]+/g, '_').replace(/\.+/g, '_')
}

function formatStamp(): string {
  return new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function ModeButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button type="button" onClick={onClick} className={`rounded-lg border px-3 py-1.5 text-sm ${active ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:bg-muted'}`}>
      {children}
    </button>
  )
}

function DownloadButton({ onClick, icon, children }: { onClick: () => void; icon: ReactNode; children: string }) {
  return (
    <button type="button" onClick={onClick} className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted">
      {icon}
      {children}
    </button>
  )
}

function TextArea({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (value: string) => void; placeholder: string }) {
  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium">{label}</label>
      <textarea value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} rows={2} className="min-h-10 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring/20" />
    </div>
  )
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return (
    <div>
      <label className="mb-1.5 block text-sm font-medium">{label}</label>
      <input type="number" value={value} min={min} max={max} onChange={(e) => onChange(Number(e.target.value))} className="h-10 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring/20" />
    </div>
  )
}

function DatasetSelect({ datasets, activeIndex, onChange, label }: { datasets: ExportDataset[]; activeIndex: number; onChange: (index: number) => void; label: string }) {
  if (datasets.length <= 1) return <span className="text-sm font-medium">{datasets[0]?.symbol}</span>
  return (
    <label className="flex items-center gap-2 text-sm">
      {label}
      <select value={activeIndex} onChange={(e) => onChange(Number(e.target.value))} className="rounded-lg border border-border bg-background px-2 py-1.5">
        {datasets.map((dataset, index) => <option key={dataset.symbol} value={index}>{dataset.symbol}</option>)}
      </select>
    </label>
  )
}

function BatchStatus({ rows, okText, failedText, rowsText }: { rows: BatchResult[]; okText: string; failedText: string; rowsText: string }) {
  return (
    <div className="mb-4 overflow-hidden rounded-lg border border-border">
      <table className="w-full text-sm">
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol} className="border-t border-border first:border-t-0">
              <td className="px-3 py-2 font-mono">{row.symbol}</td>
              <td className={row.status === 'ok' ? 'px-3 py-2 text-emerald-600' : 'px-3 py-2 text-red-600'}>{row.status === 'ok' ? okText : failedText}</td>
              <td className="px-3 py-2 text-muted-foreground">{row.status === 'ok' ? `${row.rows} ${rowsText}` : row.error}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PreviewTable({ rows }: { rows: ExportRow[] }) {
  if (!rows.length) return <div className="p-6 text-sm text-muted-foreground">No preview</div>
  const headers = Object.keys(rows[0]!)
  return (
    <div className="h-full overflow-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-muted/80 backdrop-blur">
          <tr>{headers.map((key) => <th key={key} className="whitespace-nowrap px-3 py-2 text-left font-medium">{key}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-border hover:bg-muted/20">
              {headers.map((key) => <td key={key} className="whitespace-nowrap px-3 py-2">{formatCell(row[key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EmptyState({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="flex flex-1 items-center justify-center text-muted-foreground">
      <div className="text-center">
        <FileSpreadsheet className="mx-auto mb-3" size={38} />
        <p className="text-sm">{title}</p>
        <p className="mt-1 text-xs">{subtitle}</p>
      </div>
    </div>
  )
}

function formatCell(value: ExportRow[string] | undefined): string {
  return typeof value === 'number' ? value.toFixed(4).replace(/\.?0+$/, '') : String(value ?? '')
}
