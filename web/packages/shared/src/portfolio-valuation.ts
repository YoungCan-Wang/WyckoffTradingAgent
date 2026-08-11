import type { SupabaseClient } from '@supabase/supabase-js'
import { normalizePortfolioCode, normalizeTickFlowSymbol } from './agent-market'

const ECB_RATES_URL = 'https://api.frankfurter.dev/v2/rates'

export interface PortfolioEquityRefresh {
  ok: boolean
  totalEquity: number | null
  message: string
}

interface RefreshDeps {
  supabase: SupabaseClient
  fetch: typeof globalThis.fetch
}

interface RefreshOptions {
  quoteEndpoint?: string
  cnyRates?: Partial<Record<'HKD' | 'USD', number>>
}

interface ValuationPosition {
  code: string
  shares: number
}

export async function refreshPortfolioTotalEquity(
  deps: RefreshDeps,
  userId: string,
  options: RefreshOptions = {},
): Promise<PortfolioEquityRefresh> {
  try {
    const portfolioId = `USER_LIVE:${userId}`
    const [portfolio, positionsResult] = await Promise.all([
      deps.supabase.from('portfolios').select('free_cash').eq('portfolio_id', portfolioId).single(),
      deps.supabase.from('portfolio_positions').select('code, shares').eq('portfolio_id', portfolioId),
    ])
    const readError = portfolio.error?.message || positionsResult.error?.message
    if (readError) return failedRefresh(readError)
    const positions = normalizePositions(positionsResult.data || [])
    const totalEquity = positions.length === 0
      ? roundMoney(Number(portfolio.data?.free_cash || 0))
      : await valuePositions(deps, userId, Number(portfolio.data?.free_cash || 0), positions, options)
    const updated = await deps.supabase
      .from('portfolios')
      .update({ total_equity: totalEquity, updated_at: new Date().toISOString() })
      .eq('portfolio_id', portfolioId)
    if (updated.error) return failedRefresh(updated.error.message)
    return { ok: true, totalEquity, message: `总权益已刷新为 ¥${totalEquity.toLocaleString()}` }
  } catch (error) {
    return failedRefresh(error instanceof Error ? error.message : String(error || '未知错误'))
  }
}

async function valuePositions(
  deps: RefreshDeps,
  userId: string,
  freeCash: number,
  positions: ValuationPosition[],
  options: RefreshOptions,
): Promise<number> {
  const apiKey = await loadTickFlowKey(deps.supabase, userId)
  if (!apiKey) throw new Error('未配置 TickFlow API Key')
  const prices = await fetchPrices(deps.fetch, apiKey, positions, options.quoteEndpoint)
  const currencies = new Set(positions.map((position) => portfolioCurrency(position.code)))
  const rates = await loadCnyRates(deps.fetch, currencies, options.cnyRates)
  let positionsValue = 0
  const missing: string[] = []
  for (const position of positions) {
    const price = prices[position.code] || 0
    const rate = rates[portfolioCurrency(position.code)] || 0
    if (price <= 0 || rate <= 0) missing.push(position.code)
    else positionsValue += position.shares * price * rate
  }
  if (missing.length > 0) throw new Error(`缺少完整行情或汇率: ${missing.join(', ')}`)
  return roundMoney(freeCash + positionsValue)
}

async function loadTickFlowKey(supabase: SupabaseClient, userId: string): Promise<string> {
  const result = await supabase
    .from('user_settings')
    .select('tickflow_api_key')
    .eq('user_id', userId)
    .single()
  return String(result.data?.tickflow_api_key || '').trim()
}

async function fetchPrices(
  fetcher: typeof globalThis.fetch,
  apiKey: string,
  positions: ValuationPosition[],
  quoteEndpoint = '/api/llm-proxy/v1/quotes',
): Promise<Record<string, number>> {
  const requested = new Map(positions.map((position) => [normalizeTickFlowSymbol(position.code), position.code]))
  const separator = quoteEndpoint.includes('?') ? '&' : '?'
  const url = `${quoteEndpoint}${separator}symbols=${encodeURIComponent([...requested.keys()].join(','))}`
  const response = await fetcher(url, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (!response.ok) throw new Error(`TickFlow 返回 HTTP ${response.status}`)
  const payload = await response.json() as { data?: Record<string, unknown>[] }
  const prices: Record<string, number> = {}
  for (const row of payload.data || []) {
    const code = requested.get(String(row.symbol || '').toUpperCase())
    const price = quotePrice(row)
    if (code && price > 0) prices[code] = price
  }
  return prices
}

async function loadCnyRates(
  fetcher: typeof globalThis.fetch,
  currencies: Set<string>,
  overrides: RefreshOptions['cnyRates'],
): Promise<Record<string, number>> {
  const rates: Record<string, number> = { CNY: 1 }
  const missing = [...currencies].filter((currency) => currency !== 'CNY' && !positive(overrides?.[currency as 'HKD' | 'USD']))
  for (const currency of currencies) {
    const override = overrides?.[currency as 'HKD' | 'USD']
    if (positive(override)) rates[currency] = override!
  }
  if (missing.length === 0) return rates
  const query = new URLSearchParams({ base: 'CNY', quotes: missing.join(','), providers: 'ECB' })
  const response = await fetcher(`${ECB_RATES_URL}?${query}`)
  if (!response.ok) throw new Error(`ECB 汇率返回 HTTP ${response.status}`)
  const rows = await response.json() as { quote?: string; rate?: number }[]
  for (const row of Array.isArray(rows) ? rows : []) {
    const currency = String(row.quote || '').toUpperCase()
    if (missing.includes(currency) && positive(row.rate)) rates[currency] = 1 / Number(row.rate)
  }
  return rates
}

function normalizePositions(rows: { code?: unknown; shares?: unknown }[]): ValuationPosition[] {
  return rows.flatMap((row) => {
    const code = normalizePortfolioCode(String(row.code || ''))
    const shares = Number(row.shares || 0)
    return code && Number.isInteger(shares) && shares > 0 ? [{ code, shares }] : []
  })
}

function portfolioCurrency(code: string): 'CNY' | 'HKD' | 'USD' {
  if (code.endsWith('.HK')) return 'HKD'
  if (code.endsWith('.US')) return 'USD'
  return 'CNY'
}

function quotePrice(row: Record<string, unknown>): number {
  for (const key of ['last_price', 'close', 'last', 'price', 'current']) {
    const value = Number(row[key] || 0)
    if (Number.isFinite(value) && value > 0) return value
  }
  return 0
}

function positive(value: number | undefined): boolean {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

function roundMoney(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100
}

function failedRefresh(message: string): PortfolioEquityRefresh {
  return { ok: false, totalEquity: null, message: `总权益刷新失败：${message}` }
}
