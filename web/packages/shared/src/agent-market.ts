export interface FundamentalMetric {
  symbol?: string
  period_end?: string
  announce_date?: string
  eps_basic?: number
  bps?: number
  ocfps?: number
  roe?: number
  roe_diluted?: number
  revenue_yoy?: number
  net_income_yoy?: number
  gross_margin?: number
  net_margin?: number
  debt_to_asset_ratio?: number
  operating_cash_to_revenue?: number
  inventory_turnover?: number
}

export type ValueSnapshotReason = 'unsupported-market' | 'missing-source' | 'not-found'

export interface ValueSnapshot {
  symbol: string
  source: 'tickflow' | 'tushare' | 'none'
  metrics: FundamentalMetric | null
  reason?: ValueSnapshotReason
  fetched_at?: string
}

export const TICKFLOW_PURCHASE = 'https://tickflow.org/auth/register?ref=5N4NKTCPL4'

type Fetcher = typeof globalThis.fetch

export function normalizeCode(code: string | number): string {
  const raw = String(code ?? '').trim().toUpperCase()
  return /^\d+$/.test(raw) && raw.length < 6 ? raw.padStart(6, '0') : raw
}

export function isCnSymbol(code: string): boolean {
  return /^\d{6}$/.test(code.trim())
}

export function isTickFlowMarketSymbol(code: string): boolean {
  const c = code.trim().toUpperCase()
  return /^\d{5}\.HK$/.test(c) || /^[A-Z][A-Z0-9.-]{0,15}\.US$/.test(c)
}

export function isSupportedKlineCode(code: string): boolean {
  return isCnSymbol(code) || isTickFlowMarketSymbol(code)
}

/** Normalize holdings codes: CN 6-digit, HK NNNNN.HK, US TICKER.US. Empty if unsupported.
 * Does not pad short digit strings into fake A-share codes (e.g. "6881" stays invalid). */
export function normalizePortfolioCode(raw: string | number): string {
  const c = String(raw ?? '').trim().toUpperCase()
  if (!c) return ''
  if (isCnSymbol(c)) return c
  const hk = c.match(/^(\d{1,5})\.HK$/)
  if (hk) return `${hk[1]!.padStart(5, '0')}.HK`
  if (isTickFlowMarketSymbol(c)) return c
  if (/^[A-Z][A-Z0-9.-]{0,15}$/.test(c)) {
    const withUs = `${c}.US`
    return isTickFlowMarketSymbol(withUs) ? withUs : ''
  }
  return ''
}

export function isSupportedPortfolioCode(code: string | number): boolean {
  return Boolean(normalizePortfolioCode(code))
}

export function detectMarket(code: string): 'cn' | 'hk' | 'us' {
  const c = code.trim().toUpperCase()
  if (isCnSymbol(c)) return 'cn'
  if (/^\d{5}\.HK$/.test(c)) return 'hk'
  return 'us'
}

export function normalizeTickFlowSymbol(code: string): string {
  const c = code.trim().toUpperCase()
  if (isTickFlowMarketSymbol(c)) return c
  if (!isCnSymbol(c)) return c
  if (c.startsWith('0') || c.startsWith('1') || c.startsWith('2') || c.startsWith('3')) return `${c}.SZ`
  if (c.startsWith('4') || c.startsWith('8') || c.startsWith('9')) return `${c}.BJ`
  return `${c}.SH`
}

export function normalizeTushareCode(code: string): string {
  const c = code.replace(/\.\w+$/, '')
  if (!/^\d{6}$/.test(c)) return code
  if (c.startsWith('6') || c.startsWith('5')) return `${c}.SH`
  if (c.startsWith('4') || c.startsWith('8') || c.startsWith('9')) return `${c}.BJ`
  return `${c}.SZ`
}

export async function fetchValueSnapshotWithFetch(
  fetcher: Fetcher,
  code: string,
  keys: { tickflow: string | null; tushare: string | null },
): Promise<ValueSnapshot> {
  const symbol = isCnSymbol(code) ? normalizeTickFlowSymbol(code) : code.trim().toUpperCase()
  const fetched_at = new Date().toISOString()
  if (!isCnSymbol(code)) return { symbol, source: 'none', metrics: null, reason: 'unsupported-market', fetched_at }
  if (!keys.tickflow && !keys.tushare) return { symbol, source: 'none', metrics: null, reason: 'missing-source', fetched_at }
  const tickflow = keys.tickflow ? await tryTickFlowSnapshot(fetcher, code, keys.tickflow) : null
  if (tickflow) return { symbol, source: 'tickflow', metrics: tickflow, fetched_at }
  const tushare = keys.tushare ? await tryTushareSnapshot(fetcher, code, keys.tushare) : null
  if (tushare) return { symbol, source: 'tushare', metrics: tushare, fetched_at }
  return { symbol, source: 'none', metrics: null, reason: 'not-found', fetched_at }
}

async function tryTickFlowSnapshot(fetcher: Fetcher, code: string, apiKey: string): Promise<FundamentalMetric | null> {
  try {
    const symbol = normalizeTickFlowSymbol(code)
    const params = new URLSearchParams({ symbols: symbol, latest: 'true' })
    const resp = await fetcher(`/api/llm-proxy/v1/financials/metrics?${params}`, {
      headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
    })
    if (!resp.ok) return null
    const record = findFinancialRecord(await resp.json(), symbol)
    return record ? normalizeFinancialRecord(record, symbol) : null
  } catch {
    return null
  }
}

async function tryTushareSnapshot(fetcher: Fetcher, code: string, token: string): Promise<FundamentalMetric | null> {
  try {
    const tsCode = normalizeTushareCode(code)
    const fields = 'ts_code,end_date,ann_date,eps,bps,cfps,roe,roe_dt,or_yoy,netprofit_yoy,grossprofit_margin,netprofit_margin,debt_to_assets,ocf_to_or,inv_turn'
    const json = await tusharePostWithFetch(fetcher, token, 'fina_indicator', { ts_code: tsCode }, fields)
    const items = json?.data?.items
    const fieldNames = json?.data?.fields
    if (!Array.isArray(items) || !Array.isArray(fieldNames) || items.length === 0) return null
    const row = items[0]!
    return normalizeFinancialRecord(Object.fromEntries(fieldNames.map((field, index) => [field, row[index]])), tsCode)
  } catch {
    return null
  }
}

async function tusharePostWithFetch(
  fetcher: Fetcher,
  token: string,
  api_name: string,
  params: Record<string, string>,
  fields: string,
) {
  const resp = await fetcher('/api/llm-proxy/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Target-URL': 'https://api.tushare.pro' },
    body: JSON.stringify({ api_name, token, params, fields }),
  })
  if (!resp.ok) return null
  return (await resp.json()) as { data?: { fields?: string[]; items?: unknown[][] } }
}

function normalizeFinancialRecord(record: Record<string, unknown>, fallbackSymbol: string): FundamentalMetric | null {
  const metrics: FundamentalMetric = {
    symbol: String(pickMetricValue(record, ['symbol', 'ts_code', 'code']) || fallbackSymbol),
    period_end: normalizeReportDate(pickMetricValue(record, ['period_end', 'end_date', 'report_date'])),
    announce_date: normalizeReportDate(pickMetricValue(record, ['announce_date', 'ann_date'])),
    eps_basic: finiteNumber(pickMetricValue(record, ['eps_basic', 'eps', 'basic_eps'])),
    bps: finiteNumber(pickMetricValue(record, ['bps'])),
    ocfps: finiteNumber(pickMetricValue(record, ['ocfps', 'cfps'])),
    roe: finiteNumber(pickMetricValue(record, ['roe', 'roe_weighted'])),
    roe_diluted: finiteNumber(pickMetricValue(record, ['roe_diluted', 'roe_dt'])),
    revenue_yoy: finiteNumber(pickMetricValue(record, ['revenue_yoy', 'or_yoy'])),
    net_income_yoy: finiteNumber(pickMetricValue(record, ['net_income_yoy', 'netprofit_yoy'])),
    gross_margin: finiteNumber(pickMetricValue(record, ['gross_margin', 'grossprofit_margin'])),
    net_margin: finiteNumber(pickMetricValue(record, ['net_margin', 'netprofit_margin'])),
    debt_to_asset_ratio: finiteNumber(pickMetricValue(record, ['debt_to_asset_ratio', 'debt_to_assets', 'debt_ratio'])),
    operating_cash_to_revenue: finiteNumber(pickMetricValue(record, ['operating_cash_to_revenue', 'ocf_to_or'])),
    inventory_turnover: finiteNumber(pickMetricValue(record, ['inventory_turnover', 'inv_turn'])),
  }
  return hasNumericMetric(metrics) ? metrics : null
}

export function pickMetricValue(row: Record<string, unknown>, aliases: string[]): unknown {
  for (const alias of aliases) {
    if (Object.prototype.hasOwnProperty.call(row, alias)) return row[alias]
    const upper = alias.toUpperCase()
    if (Object.prototype.hasOwnProperty.call(row, upper)) return row[upper]
    const lower = alias.toLowerCase()
    if (Object.prototype.hasOwnProperty.call(row, lower)) return row[lower]
  }
  return undefined
}

export function normalizeReportDate(value: unknown): string | undefined {
  const raw = String(value || '').trim()
  if (!raw) return undefined
  if (/^\d{8}$/.test(raw)) return raw.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3')
  if (/^\d{4}-\d{2}-\d{2}/.test(raw)) return raw.slice(0, 10)
  if (/^\d{13}$/.test(raw)) return new Date(Number(raw)).toISOString().slice(0, 10)
  return raw.slice(0, 10)
}

export function finiteNumber(value: unknown): number | undefined {
  if (value == null || value === '') return undefined
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : undefined
}

function hasNumericMetric(metrics: FundamentalMetric): boolean {
  return Object.entries(metrics).some(([key, value]) =>
    key !== 'symbol' && key !== 'period_end' && key !== 'announce_date' && typeof value === 'number')
}

export function firstFinancialObject(value: unknown): Record<string, unknown> | null {
  if (!value) return null
  if (Array.isArray(value)) return value.find((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object' && !Array.isArray(row)) ?? null
  if (typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>
  return null
}

export function looksLikeFinancialRecord(row: Record<string, unknown>): boolean {
  return [
    'roe', 'ROE', 'roe_weighted', 'ROE_WEIGHTED',
    'net_income_yoy', 'NET_INCOME_YOY', 'netprofit_yoy',
    'gross_margin', 'GROSS_MARGIN', 'grossprofit_margin',
    'debt_to_asset_ratio', 'DEBT_TO_ASSET_RATIO', 'debt_to_assets',
  ].some((field) => Object.prototype.hasOwnProperty.call(row, field))
}

export function findFinancialRecord(payload: unknown, symbol: string): Record<string, unknown> | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const root = payload as Record<string, unknown>
  const data = root.data ?? root
  const direct = firstFinancialObject(data)
  if (direct && (Array.isArray(data) || looksLikeFinancialRecord(direct))) return direct
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const table = data as Record<string, unknown>
  const directBySymbol = firstFinancialObject(table[symbol])
  if (directBySymbol) return directBySymbol
  for (const value of Object.values(table)) {
    const row = firstFinancialObject(value)
    if (row) return row
  }
  return null
}
