import type { SupabaseClient } from '@supabase/supabase-js'
import { Output } from 'ai'
import type { generateText as GenerateTextFn } from 'ai'
import { z } from 'zod'
import { fetchValueSnapshotWithFetch, isCnSymbol, normalizeCode, normalizeTickFlowSymbol, normalizeTushareCode, type ValueSnapshot } from './agent-market'
import { buildValuePrompt, buildValueScore } from './agent-value'
import {
  attributionFormalDynamicLabel,
  attributionGovernorStatusLabel,
  attributionModeRecommendationLabel,
  attributionNextActionLabel,
  attributionOperatorSummary as buildAttributionOperatorSummary,
  attributionPromotionStatusLabel,
  checklistKeyLabel,
  checklistStatusLabel,
} from './attribution-summary'
import { formatPatternReviewDigest, labelCandidateTerm, type PatternReviewRow } from './pattern-review'
import { ANALYSIS_CONTEXT_PACK_SCHEMA, buildStockAnalysisContextPack } from './analysis-context'
import { marketWatchSymbol, normalizeMarketWatchCode, readFreshMarketWatchSnapshot, type MarketWatchQuote, type MarketWatchSnapshot } from './market-watch'

export interface KlineRow {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface ToolDeps {
  supabase: SupabaseClient
  fetch: typeof globalThis.fetch
  generateText: typeof GenerateTextFn
}

export interface LLMToolConfig {
  api_key: string
  model: string
  base_url: string
}

export type KlineDataSource = 'tickflow' | 'tushare' | 'mixed' | 'none'

export interface KlineDataQuality {
  source: KlineDataSource
  latestTradingDate: string | null
  coverageStart: string | null
  coverageEnd: string | null
  requestedRows: number
  returnedRows: number
  isComplete: boolean
  fallbackUsed: boolean
}

export function buildKlineDataQuality(
  source: KlineDataSource,
  requestedRows: number,
  rows: KlineRow[],
  fallbackUsed = false,
): KlineDataQuality {
  const dates = rows.map((row) => row.date).filter(Boolean).sort()
  return {
    source,
    latestTradingDate: dates.at(-1) || null,
    coverageStart: dates[0] || null,
    coverageEnd: dates.at(-1) || null,
    requestedRows,
    returnedRows: rows.length,
    isComplete: rows.length >= requestedRows,
    fallbackUsed,
  }
}

export const ANALYZE_STOCK_OUTPUT_SCHEMA = z.object({
  summary: z.string(),
  phase: z.string(),
  confidence: z.number().nullable(),
  support: z.string().nullable(),
  resistance: z.string().nullable(),
  action: z.string(),
  risk: z.string(),
  markdown: z.string(),
  data_source: z.string().nullable().optional(),
  data_asof: z.string().nullable().optional(),
  data_quality: z.object({
    source: z.enum(['tickflow', 'tushare', 'mixed', 'none']),
    latestTradingDate: z.string().nullable(),
    coverageStart: z.string().nullable(),
    coverageEnd: z.string().nullable(),
    requestedRows: z.number(),
    returnedRows: z.number(),
    isComplete: z.boolean(),
    fallbackUsed: z.boolean(),
  }).nullable().optional(),
  context_pack: ANALYSIS_CONTEXT_PACK_SCHEMA.nullable().optional(),
})

export const STRATEGY_POLICY_OUTPUT_SCHEMA = z.object({
  dynamic_mode: z.string().nullable().optional(),
  dynamic_mode_label: z.string().nullable().optional(),
  execution_policy: z.string().nullable().optional(),
  execution_policy_label: z.string().nullable().optional(),
  policy_weight_active_scope: z.string().nullable().optional(),
  selection_action_count: z.number().nullable().optional(),
  selection_action_summary: z.string().nullable().optional(),
  formal_dynamic_allowed: z.boolean().nullable().optional(),
  next_action: z.string().nullable().optional(),
  next_action_label: z.string().nullable().optional(),
  signal_weights: z.record(z.number()).nullable().optional(),
  attribution_signal_weights: z.record(z.number()).nullable().optional(),
})

export const STRATEGY_DECISION_OUTPUT_SCHEMA = z.object({
  summary: z.string(),
  market_regime: z.string(),
  overall_position: z.string(),
  risk: z.string(),
  position_actions: z.array(z.object({
    code: z.string(),
    name: z.string().nullable(),
    action: z.string(),
    reason: z.string(),
    risk: z.string(),
  })),
  strategy_policy: STRATEGY_POLICY_OUTPUT_SCHEMA.nullable().optional(),
})

export type AnalyzeStockResult = z.infer<typeof ANALYZE_STOCK_OUTPUT_SCHEMA>
export type ScreenStrategyPolicy = z.infer<typeof STRATEGY_POLICY_OUTPUT_SCHEMA>
export type StrategyDecisionResult = z.infer<typeof STRATEGY_DECISION_OUTPUT_SCHEMA>

export function buildKlineDigest(data: KlineRow[]): string {
  if (data.length === 0) return '无可用K线数据'
  const last = data[data.length - 1]!
  const avg = (arr: number[]) => arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : 0
  const slice = (n: number) => data.slice(-n)
  const ma = (n: number) => avg(slice(n).map(d => d.close))
  const vol = (n: number) => avg(slice(n).map(d => d.volume))
  const p20 = slice(20)

  const lines = [
    `K线共${data.length}根，最新日期 ${last.date}`,
    `最新收盘 ${last.close.toFixed(2)}，开盘 ${last.open.toFixed(2)}，高 ${last.high.toFixed(2)}，低 ${last.low.toFixed(2)}`,
    `MA5=${ma(5).toFixed(2)} MA10=${ma(10).toFixed(2)} MA20=${ma(20).toFixed(2)}`,
  ]
  if (data.length >= 50) lines.push(`MA50=${ma(50).toFixed(2)}`)
  if (data.length >= 120) lines.push(`MA120=${ma(120).toFixed(2)}`)
  lines.push(
    `近20日最高 ${Math.max(...p20.map(d => d.high)).toFixed(2)}，最低 ${Math.min(...p20.map(d => d.low)).toFixed(2)}`,
    `近5日均量 ${vol(5).toFixed(0)}，近20日均量 ${vol(20).toFixed(0)}`,
    `量比(5/20) ${(vol(5) / (vol(20) || 1)).toFixed(2)}`,
  )

  const recent5 = slice(5)
  lines.push('近5日走势: ' + recent5.map(d => {
    const chg = ((d.close - d.open) / d.open * 100).toFixed(1)
    return `${d.date.slice(5)} ${Number(chg) >= 0 ? '+' : ''}${chg}%`
  }).join(' → '))

  return lines.join('\n')
}

export async function fetchUserDataKeys(deps: ToolDeps, userId: string): Promise<{ tickflow: string | null; tushare: string | null }> {
  const { data } = await deps.supabase
    .from('user_settings')
    .select('tickflow_api_key, tushare_token')
    .eq('user_id', userId)
    .single()
  return {
    tickflow: String(data?.tickflow_api_key || '').trim() || null,
    tushare: String(data?.tushare_token || '').trim() || null,
  }
}

export async function fetchTickFlowKey(deps: ToolDeps, userId: string): Promise<string | null> {
  const keys = await fetchUserDataKeys(deps, userId)
  return keys.tickflow
}

type RawMarketWatchQuote = Record<string, unknown>

export async function fetchMarketWatchSnapshot(
  deps: ToolDeps,
  userId: string,
  codes: string[],
  cached: unknown = null,
): Promise<MarketWatchSnapshot> {
  const requestedCodes = Array.from(new Set(codes
    .map((code) => normalizeMarketWatchCode(code))
    .filter((code) => code.length > 0 && code.length <= 24))).slice(0, 18)
  const fetchedAt = new Date().toISOString()
  if (requestedCodes.length === 0) {
    return { state: 'empty', source: 'none', requestedCodes, quotes: [], fetchedAt, fromCache: false }
  }
  const cachedSnapshot = readFreshMarketWatchSnapshot(cached, requestedCodes)
  if (cachedSnapshot) return cachedSnapshot

  const tickflowKey = await fetchTickFlowKey(deps, userId).catch(() => null)
  if (!tickflowKey) {
    return {
      state: 'unavailable',
      source: 'none',
      requestedCodes,
      quotes: [],
      fetchedAt,
      fromCache: false,
      message: '未配置 TickFlow API Key',
    }
  }

  try {
    const symbols = requestedCodes.map(marketWatchSymbol).join(',')
    const response = await deps.fetch(`/api/llm-proxy/v1/quotes?symbols=${encodeURIComponent(symbols)}`, {
      headers: { 'x-api-key': tickflowKey, 'X-Target-URL': 'https://api.tickflow.org' },
    })
    if (!response.ok) {
      return buildUnavailableMarketWatch(requestedCodes, fetchedAt, `TickFlow 返回 HTTP ${response.status}`)
    }
    const rows = parseMarketWatchRows(await response.json())
    const quotes = requestedCodes.map((requestedCode) => {
      const symbol = marketWatchSymbol(requestedCode)
      const row = rows.find((candidate) => marketWatchRowMatches(candidate, symbol, requestedCode))
      return normalizeMarketWatchQuote(requestedCode, symbol, row, fetchedAt)
    })
    const available = quotes.filter((quote) => quote.price != null || quote.changePct != null)
    if (available.length === 0) return buildUnavailableMarketWatch(requestedCodes, fetchedAt, 'TickFlow 没有返回观察篮的可用报价')
    return { state: 'ready', source: 'tickflow', requestedCodes, quotes, fetchedAt, fromCache: false }
  } catch {
    return buildUnavailableMarketWatch(requestedCodes, fetchedAt, 'TickFlow 行情请求失败')
  }
}

function buildUnavailableMarketWatch(requestedCodes: string[], fetchedAt: string, message: string): MarketWatchSnapshot {
  return { state: 'unavailable', source: 'none', requestedCodes, quotes: [], fetchedAt, fromCache: false, message }
}

function parseMarketWatchRows(payload: unknown): RawMarketWatchQuote[] {
  if (!payload || typeof payload !== 'object') return []
  const root = payload as Record<string, unknown>
  const data = root.data
  if (Array.isArray(data)) return data.filter(isRecord)
  if (data && typeof data === 'object') return Object.values(data).flatMap((value) => Array.isArray(value) ? value.filter(isRecord) : isRecord(value) ? [value] : [])
  if (Array.isArray(root.records)) return root.records.filter(isRecord)
  return []
}

function isRecord(value: unknown): value is RawMarketWatchQuote {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function marketWatchRowMatches(row: RawMarketWatchQuote, symbol: string, requestedCode: string): boolean {
  const rowSymbol = String(row.symbol || row.code || row.ts_code || '').trim().toUpperCase()
  return rowSymbol === symbol || rowSymbol === requestedCode || rowSymbol.split('.')[0] === requestedCode.split('.')[0]
}

function normalizeMarketWatchQuote(
  requestedCode: string,
  symbol: string,
  row: RawMarketWatchQuote | undefined,
  fallbackAsOf: string,
): MarketWatchQuote {
  const price = numberFrom(row, ['last', 'price', 'current', 'close'])
  const previousClose = numberFrom(row, ['pre_close', 'previous_close', 'prev_close'])
  const directChange = numberFrom(row, ['pct_chg', 'change_pct', 'percent_change', 'changePercent'])
  const changePct = directChange ?? (price != null && previousClose ? ((price - previousClose) / previousClose) * 100 : null)
  return {
    requestedCode,
    symbol,
    price,
    changePct,
    previousClose,
    volume: numberFrom(row, ['volume', 'vol']),
    asOf: normalizeMarketWatchTimestamp(row?.timestamp || row?.time || row?.datetime || row?.date, fallbackAsOf),
  }
}

function normalizeMarketWatchTimestamp(value: unknown, fallback: string): string {
  if (value == null || value === '') return fallback
  const numeric = Number(value)
  if (Number.isFinite(numeric) && numeric > 0) {
    const milliseconds = numeric < 10_000_000_000 ? numeric * 1000 : numeric
    const date = new Date(milliseconds)
    if (!Number.isNaN(date.getTime())) return date.toISOString()
  }
  const date = new Date(String(value))
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString()
}

function numberFrom(row: RawMarketWatchQuote | undefined, keys: string[]): number | null {
  if (!row) return null
  for (const key of keys) {
    const value = Number(row[key])
    if (Number.isFinite(value)) return value
  }
  return null
}

async function tusharePost(deps: ToolDeps, token: string, api_name: string, params: Record<string, string>, fields: string) {
  const resp = await deps.fetch('/api/llm-proxy/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Target-URL': 'https://api.tushare.pro' },
    body: JSON.stringify({ api_name, token, params, fields }),
  })
  if (!resp.ok) return null
  return (await resp.json()) as { data?: { fields?: string[]; items?: unknown[][] } }
}

async function fetchKlineViaTushare(deps: ToolDeps, code: string, token: string, startDate: string, endDate: string): Promise<KlineRow[]> {
  const tsCode = normalizeTushareCode(code)
  const [dailyJson, adjJson] = await Promise.all([
    tusharePost(deps, token, 'daily', { ts_code: tsCode, start_date: startDate, end_date: endDate }, 'trade_date,open,high,low,close,vol'),
    tusharePost(deps, token, 'adj_factor', { ts_code: tsCode, start_date: startDate, end_date: endDate }, 'trade_date,adj_factor'),
  ])
  const items = dailyJson?.data?.items
  if (!Array.isArray(items) || items.length === 0) return []

  const adjItems = adjJson?.data?.items
  if (!Array.isArray(adjItems) || adjItems.length === 0) return []
  const adjMap = new Map<string, number>()
  let latestDate = ''
  for (const row of adjItems) {
    const dt = String(row[0])
    adjMap.set(dt, Number(row[1]))
    if (dt > latestDate) latestDate = dt
  }
  const latestFactor = adjMap.get(latestDate) || 1

  return items.map(row => {
    const dt = String(row[0] || '')
    const factor = adjMap.get(dt)
    if (!factor) return null
    const ratio = factor / latestFactor
    return {
      date: dt.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
      open: Number(row[1] || 0) * ratio, high: Number(row[2] || 0) * ratio,
      low: Number(row[3] || 0) * ratio, close: Number(row[4] || 0) * ratio,
      volume: Number(row[5] || 0),
    }
  }).filter((d): d is KlineRow => d !== null && d.date !== '' && d.close > 0)
}

function parseKlineRows(rows: unknown[]): KlineRow[] {
  return (rows as Record<string, unknown>[]).map(r => ({
    date: String(r.date || r.trade_date || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
    open: Number(r.open || 0),
    high: Number(r.high || 0),
    low: Number(r.low || 0),
    close: Number(r.close || 0),
    volume: Number(r.volume || r.vol || 0),
  })).filter(d => d.date && d.close > 0)
}

function parseTickFlowTable(table: Record<string, unknown[]>): KlineRow[] {
  const ts = Array.isArray(table.timestamp) ? table.timestamp : []
  if (ts.length === 0) return []
  const o = table.open || [], h = table.high || [], l = table.low || [], c = table.close || [], v = table.volume || []
  return ts.map((t, i) => ({
    date: formatTimestamp(t), open: Number(o[i] || 0), high: Number(h[i] || 0),
    low: Number(l[i] || 0), close: Number(c[i] || 0), volume: Number(v[i] || 0),
  })).filter(d => d.date && d.close > 0)
}

function findTickFlowTable(data: unknown, symbol: string): Record<string, unknown[]> | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const obj = data as Record<string, unknown>
  if (Array.isArray(obj.timestamp)) return obj as Record<string, unknown[]>
  const direct = obj[symbol]
  if (direct && typeof direct === 'object' && !Array.isArray(direct)) {
    const table = direct as Record<string, unknown>
    if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
  }
  for (const value of Object.values(obj)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const table = value as Record<string, unknown>
      if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
    }
  }
  return null
}

function parseTickFlowPayload(json: Record<string, unknown>, symbol: string): KlineRow[] {
  const data = json.data
  if (Array.isArray(data)) return parseKlineRows(data)
  if (Array.isArray(json.records)) return parseKlineRows(json.records)
  const table = findTickFlowTable(data, symbol)
  return table ? parseTickFlowTable(table) : []
}

function formatTimestamp(value: unknown): string {
  const n = Number(value)
  if (Number.isFinite(n) && n > 0) return new Date(n + 8 * 3600_000).toISOString().slice(0, 10)
  return String(value || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3').slice(0, 10)
}

async function fetchKlineViaTickFlow(deps: ToolDeps, code: string, apiKey: string, count = 250): Promise<KlineRow[]> {
  const symbol = normalizeTickFlowSymbol(code)
  const params = new URLSearchParams({
    symbol, period: '1d', count: String(count), adjust: 'forward',
  })
  const resp = await deps.fetch(`/api/llm-proxy/v1/klines?${params}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (resp.ok) {
    const rows = parseTickFlowPayload(await resp.json(), symbol)
    if (rows.length) return rows
  }
  const batchParams = new URLSearchParams({ symbols: symbol, period: '1d', count: String(count), adjust: 'forward' })
  const batchResp = await deps.fetch(`/api/llm-proxy/v1/klines/batch?${batchParams}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (!batchResp.ok) return []
  return parseTickFlowPayload(await batchResp.json(), symbol)
}


export async function fetchKlineForAgentWithQuality(
  deps: ToolDeps,
  code: string,
  keys: { tickflow: string | null; tushare: string | null },
  _userId: string,
): Promise<{ rows: KlineRow[]; quality: KlineDataQuality }> {
  const end = new Date(); end.setDate(end.getDate() - 1)
  const start = new Date(); start.setDate(start.getDate() - 500)
  const fmt = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, '')
  const isCn = isCnSymbol(code)

  if (keys.tickflow) {
    try {
      const r = await fetchKlineViaTickFlow(deps, code, keys.tickflow)
      if (r.length) return { rows: r, quality: buildKlineDataQuality('tickflow', 320, r) }
    } catch { /* */ }
  }
  if (isCn && keys.tushare) {
    try {
      const r = (await fetchKlineViaTushare(deps, code, keys.tushare, fmt(start), fmt(end))).sort((a, b) => a.date.localeCompare(b.date))
      if (r.length) return { rows: r, quality: buildKlineDataQuality('tushare', 320, r, Boolean(keys.tickflow)) }
    } catch { /* */ }
  }
  return { rows: [], quality: buildKlineDataQuality('none', 320, []) }
}

export async function fetchKlineForAgent(
  deps: ToolDeps,
  code: string,
  keys: { tickflow: string | null; tushare: string | null },
  userId: string,
): Promise<KlineRow[]> {
  return (await fetchKlineForAgentWithQuality(deps, code, keys, userId)).rows
}

export async function fetchValueSnapshotForAgent(deps: ToolDeps, code: string, keys: { tickflow: string | null; tushare: string | null }): Promise<ValueSnapshot> {
  return fetchValueSnapshotWithFetch(deps.fetch, code, keys)
}

export function buildValueAgentDigest(snapshot: ValueSnapshot): string {
  const base = buildValuePrompt(snapshot)
  const score = buildValueScore(snapshot.metrics)
  if (!snapshot.metrics) return base
  const strengths = score.strengths.map((item) => item.label).join('；') || '暂无明显质量加分项'
  const risks = score.risks.map((item) => item.label).join('；') || '暂无明显价值面风险项'
  return [
    base,
    `价值面评级：${score.label}`,
    `质量信号：${strengths}`,
    `风险信号：${risks}`,
  ].join('\n')
}

export async function fetchQuotes(
  deps: ToolDeps,
  tickflowKey: string | null,
  stocks: { code: number }[],
): Promise<Record<string, Record<string, number>>> {
  if (!tickflowKey || stocks.length === 0) return {}
  try {
    const symbols = stocks.map(r => {
      const c = normalizeCode(r.code)
      if (c.startsWith('6')) return `${c}.SH`
      if (c.startsWith('4') || c.startsWith('8') || c.startsWith('9')) return `${c}.BJ`
      return `${c}.SZ`
    }).join(',')
    const resp = await deps.fetch(
      `/api/llm-proxy/v1/quotes?symbols=${symbols}`,
      { headers: { 'x-api-key': tickflowKey, 'X-Target-URL': 'https://api.tickflow.org' } },
    )
    if (!resp.ok) return {}
    const json = await resp.json() as { data?: Record<string, number>[] }
    const result: Record<string, Record<string, number>> = {}
    for (const row of (json.data || [])) {
      const sym = String((row as Record<string, unknown>).symbol || '')
      const code6 = sym.split('.')[0] || ''
      if (code6) result[code6] = row
    }
    return result
  } catch { return {} }
}

export async function execSearchStock(deps: ToolDeps, userId: string, query: string): Promise<string> {
  const q = query.trim()
  const isCode = /^\d+$/.test(q)

  const tables = ['recommendation_tracking', 'portfolio_positions'] as const
  const allRows: { code: number; name: string }[] = []

  for (const table of tables) {
    const res = isCode
      ? await deps.supabase.from(table).select('code, name').eq('code', parseInt(q)).limit(5)
      : await deps.supabase.from(table).select('code, name').ilike('name', `%${q}%`).limit(10)
    if (res.data) allRows.push(...res.data)
  }

  if (allRows.length === 0) return `未找到匹配"${query}"的股票`

  const seen = new Set<number>()
  const unique = allRows.filter((r) => {
    if (seen.has(r.code)) return false
    seen.add(r.code)
    return true
  }).slice(0, 10)

  const tickflowKey = await fetchTickFlowKey(deps, userId)
  const quotes = await fetchQuotes(deps, tickflowKey, unique)

  const lines = unique.map(r => {
    const code6 = normalizeCode(r.code)
    const qt = quotes[code6]
    if (qt) {
      const price = qt.close || qt.last || qt.price || qt.current || 0
      const pct = qt.pct_chg ?? ((qt.close && qt.pre_close) ? ((qt.close - qt.pre_close) / qt.pre_close * 100) : null)
      const pctStr = pct != null ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : ''
      return `${code6} ${r.name} | ¥${price.toFixed(2)} ${pctStr}`
    }
    return `${code6} ${r.name}`
  })

  return lines.join('\n')
}

export async function execViewPortfolio(deps: ToolDeps, userId: string): Promise<string> {
  const portfolioId = `USER_LIVE:${userId}`

  const [pfResult, posResult] = await Promise.all([
    deps.supabase.from('portfolios').select('free_cash').eq('portfolio_id', portfolioId).single(),
    deps.supabase.from('portfolio_positions').select('code, name, shares, cost_price, buy_dt, stop_loss').eq('portfolio_id', portfolioId),
  ])

  const cash = pfResult.data?.free_cash || 0
  const positions = posResult.data || []

  if (positions.length === 0) {
    return `当前无持仓。可用资金：¥${cash.toLocaleString()}`
  }

  const lines = positions.map((p) => {
    const sl = p.stop_loss ? ` | 止损¥${p.stop_loss.toFixed(2)}` : ''
    return `${p.code} ${p.name} | ${p.shares}股 | 成本¥${p.cost_price.toFixed(2)} | 建仓${p.buy_dt || '未知'}${sl}`
  })
  const totalCost = positions.reduce((s, p) => s + p.shares * p.cost_price, 0)

  return [
    `持仓 ${positions.length} 只，可用资金 ¥${cash.toLocaleString()}，持仓成本合计 ¥${totalCost.toLocaleString()}`,
    '',
    ...lines,
  ].join('\n')
}

export async function execMarketOverview(deps: ToolDeps): Promise<string> {
  const { data } = await deps.supabase
    .from('market_signal_daily')
    .select('*')
    .order('trade_date', { ascending: false })
    .limit(3)

  if (!data || data.length === 0) return '暂无最新市场信号数据'

  const merged: Record<string, unknown> = { ...data[0] }
  for (const row of data) {
    for (const key of ['benchmark_regime', 'premarket_regime', 'main_index_close', 'main_index_today_pct']) {
      if (!merged[key] && row[key]) merged[key] = row[key]
    }
    for (const key of ['a50_close', 'a50_pct_chg']) {
      if (!merged[key] && row[key]) merged[key] = row[key]
    }
    for (const key of ['vix_close', 'vix_pct_chg']) {
      if (!merged[key] && row[key]) merged[key] = row[key]
    }
  }

  const tradeDate = String(data[0]!.trade_date || '')
  const regimeMap: Record<string, string> = {
    RISK_ON: '过热禁追', BEAR_REBOUND: '反抽观察', NEUTRAL: '中性', NORMAL: '常态', CAUTION: '谨慎确认', RISK_OFF: '偏弱', CRASH: '极弱', BLACK_SWAN: '恶劣', UNKNOWN: '待确认',
  }
  const regime = String(merged.benchmark_regime || 'UNKNOWN').toUpperCase()
  const premarket = String(merged.premarket_regime || 'UNKNOWN').toUpperCase()
  const blockedRegimes = new Set(['UNKNOWN', 'RISK_ON', 'BEAR_REBOUND', 'PANIC_REPAIR', 'RISK_OFF', 'CRASH', 'BLACK_SWAN'])
  const hardPremarket = new Set(['UNKNOWN', 'RISK_OFF', 'BLACK_SWAN'])
  const blocked = blockedRegimes.has(regime) || hardPremarket.has(premarket)
  const executionGate = blocked
    ? '执行闸门：禁止新开仓，只管理已有仓位'
    : premarket === 'CAUTION' || regime === 'CAUTION'
      ? '执行闸门：仅允许二次确认后的 PROBE，禁止 ATTACK'
      : '执行闸门：允许 confirmed 候选进入 OMS 复核'
  const close = Number(merged.main_index_close || 0)
  const pct = Number(merged.main_index_today_pct || 0)
  const a50Close = Number(merged.a50_close || 0)
  const a50Pct = Number(merged.a50_pct_chg || 0)
  const vixClose = Number(merged.vix_close || 0)
  const title = String(merged.banner_title || '')
  const body = String(merged.banner_message || '')
  const freshness = dataFreshnessNote(tradeDate, '市场信号')

  return [
    tradeDate ? `数据日期：${tradeDate}` : '',
    `大盘状态：${regimeMap[regime] || regime}`,
    `盘前状态：${regimeMap[premarket] || premarket}`,
    executionGate,
    close ? `上证指数：${close.toFixed(0)} (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)` : '',
    a50Close ? `A50：${a50Close.toFixed(0)} (${a50Pct >= 0 ? '+' : ''}${a50Pct.toFixed(2)}%)` : '',
    vixClose ? `VIX：${vixClose.toFixed(1)}` : '',
    title ? `\n${title}` : '',
    body ? body : '',
    freshness,
  ].filter(Boolean).join('\n')
}

type MarketIndexKey = 'sse' | 'csi300' | 'szse' | 'chinext'

const MARKET_INDEXES: Record<MarketIndexKey, { code: string; name: string }> = {
  sse: { code: '000001.SH', name: '上证指数' },
  csi300: { code: '000300.SH', name: '沪深300' },
  szse: { code: '399001.SZ', name: '深证成指' },
  chinext: { code: '399006.SZ', name: '创业板指' },
}

export async function execMarketHistory(
  deps: ToolDeps,
  userId: string,
  model: unknown,
  days = 100,
  index: MarketIndexKey = 'sse',
): Promise<string> {
  const key = await fetchTickFlowKey(deps, userId)
  if (!key) return '无法回看大盘历史K线：请先在设置页配置 TickFlow API Key。'
  const requestedDays = Math.min(Math.max(Math.trunc(days) || 100, 1), 250)
  const fetchDays = Math.max(requestedDays, 20)
  const target = MARKET_INDEXES[index] || MARKET_INDEXES.sse
  const rows = await fetchKlineViaTickFlow(deps, target.code, key, fetchDays)
  if (rows.length === 0) return `无法获取 ${target.name} 过去 ${requestedDays} 个交易日K线。请检查 TickFlow 数据权限或稍后重试。`
  const digest = buildMarketHistoryDigest(target.name, rows.slice(-requestedDays))
  const result = await deps.generateText({
    model: model as Parameters<typeof GenerateTextFn>[0]['model'],
    system: '你是威科夫大盘量价分析师。基于指数历史OHLCV，判断过去一段时间的大盘阶段、供需关系、量价背离、关键支撑压力与当前市场位置。不得只引用当天水温，不得编造数据。',
    prompt: digest,
  })
  return result.text || digest
}

function buildMarketHistoryDigest(name: string, rows: KlineRow[]): string {
  const last = rows[rows.length - 1]!
  const first = rows[0]!
  const avg = (values: number[]) => values.reduce((sum, v) => sum + v, 0) / Math.max(values.length, 1)
  const latest20 = rows.slice(-20)
  const high = Math.max(...rows.map((r) => r.high))
  const low = Math.min(...rows.map((r) => r.low))
  const ret = first.close > 0 ? (last.close / first.close - 1) * 100 : 0
  const vol5 = avg(rows.slice(-5).map((r) => r.volume))
  const vol20 = avg(latest20.map((r) => r.volume))
  const closePos = high > low ? ((last.close - low) / (high - low)) * 100 : 0
  const recent = rows.slice(-30).map((r) => [
    r.date, r.open.toFixed(2), r.high.toFixed(2), r.low.toFixed(2), r.close.toFixed(2), Math.round(r.volume),
  ].join(','))
  return [
    `指数：${name}`,
    '数据来源：TickFlow 日线K线',
    `样本：最近${rows.length}个交易日，${first.date} 至 ${last.date}`,
    `区间涨跌：${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%，区间高点 ${high.toFixed(2)}，低点 ${low.toFixed(2)}，当前区间位置 ${closePos.toFixed(1)}%`,
    `近5日均量 ${vol5.toFixed(0)}，近20日均量 ${vol20.toFixed(0)}，量比(5/20) ${(vol5 / (vol20 || 1)).toFixed(2)}`,
    '',
    '请结合以下最近30根K线判断量价关系和威科夫阶段：',
    '```csv',
    'date,open,high,low,close,volume',
    ...recent,
    '```',
  ].join('\n')
}

export async function execQueryRecommendations(deps: ToolDeps, limit: number): Promise<string> {
  const [recommendations, signals] = await Promise.all([
    fetchRecommendationReviewRows(deps, limit),
    fetchSignalPendingReviewRows(deps, limit),
  ])
  const data = recommendations.concat(signals)
    .sort((a, b) => reviewDateNumber(b.recommend_date) - reviewDateNumber(a.recommend_date))
    .slice(0, Math.max(limit, 0))
  return formatPatternReviewDigest(data)
}

async function fetchRecommendationReviewRows(deps: ToolDeps, limit: number): Promise<PatternReviewRow[]> {
  const { data } = await deps.supabase
    .from('recommendation_tracking')
    .select(
      'code, name, recommend_date, recommend_count, initial_price, current_price, change_pct, is_ai_recommended, funnel_score, candidate_lane, entry_type, signal_key, candidate_status, mainline_score',
    )
    .order('recommend_date', { ascending: false })
    .limit(limit)
  return (data ?? []).map((row) => ({ ...row, source_type: 'recommendation_tracking' }))
}

async function fetchSignalPendingReviewRows(deps: ToolDeps, limit: number): Promise<PatternReviewRow[]> {
  const { data } = await deps.supabase
    .from('signal_pending')
    .select(
      'code,name,signal_type,signal_date,status,signal_score,snap_close,candidate_lane,entry_type,signal_key,candidate_status,mainline_score',
    )
    .in('status', ['pending', 'confirmed'])
    .order('signal_date', { ascending: false })
    .limit(limit)
  return (data ?? []).map(mapSignalPendingReviewRow).filter((row): row is PatternReviewRow => row !== null)
}

function mapSignalPendingReviewRow(row: Record<string, unknown>): PatternReviewRow | null {
  const recommendDate = signalDateNumber(row.signal_date)
  if (!recommendDate) return null
  const signalType = stringOrNull(row.signal_type)
  const status = stringOrNull(row.status) || 'pending'
    return {
      code: normalizeCode(row.code as string | number),
      name: stringOrNull(row.name) || normalizeCode(row.code as string | number),
    recommend_date: recommendDate,
    recommend_count: 1,
    initial_price: numberOrNull(row.snap_close),
    current_price: null,
    change_pct: null,
    is_ai_recommended: false,
    candidate_lane: stringOrNull(row.candidate_lane) || signalType,
    entry_type: stringOrNull(row.entry_type),
    signal_key: stringOrNull(row.signal_key) || signalType,
    candidate_status: stringOrNull(row.candidate_status) || status,
    mainline_score: numberOrNull(row.mainline_score),
    source_type: 'signal_pending',
    signal_status: status,
    signal_type: signalType,
  }
}

function signalDateNumber(value: unknown): number {
  const digits = String(value || '').replaceAll('-', '')
  return /^\d{8}$/.test(digits) ? Number(digits) : 0
}

function reviewDateNumber(value: string | number): number {
  if (typeof value === 'number') return Number.isFinite(value) ? value : 0
  return signalDateNumber(value)
}

function stringOrNull(value: unknown): string | null {
  const text = String(value ?? '').trim()
  return text ? text : null
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function jsonMapOrNull(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== 'string' || value.trim() === '') return null
  try {
    const parsed = JSON.parse(value) as unknown
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null
  } catch {
    return null
  }
}

export async function execQueryAttribution(deps: ToolDeps, limit: number): Promise<string> {
  const { data } = await deps.supabase
    .from('strategy_attribution_reports')
    .select('report_date,window_start,window_end,shadow_diff_stats_json,recommendations_json')
    .eq('market', 'cn')
    .order('report_date', { ascending: false })
    .limit(Math.max(Math.trunc(limit) || 1, 1))

  if (!data || data.length === 0) {
    return '暂无策略归因报告；Web 只读取远端 strategy_attribution_reports，本地 --no-write 报告请用 CLI/MCP 的 query_history(source="attribution") 查看。'
  }
  const latestDate = String(data[0]!.report_date || '')
  const freshness = dataFreshnessNote(latestDate, '策略归因')
  const body = data.map(formatAttributionReport).join('\n\n---\n\n')
  return freshness ? `${freshness}\n\n${body}` : body
}

function formatAttributionReport(row: Record<string, unknown>): string {
  const shadow = jsonMapOrNull(row.shadow_diff_stats_json) || {}
  const governor = jsonMapOrNull(shadow.policy_governor) || {}
  const execution = withAttributionActiveScope(
    jsonMapOrNull(shadow.policy_execution_state) || attributionExecutionFallback(governor, row.recommendations_json),
  )
  const operations = jsonMapOrNull(shadow.policy_operations_brief) || {}
  const latest = jsonMapOrNull(shadow.latest) || {}
  const actions = jsonArray(row.recommendations_json).filter(isSignalAction).slice(0, 8)
  return [
    `策略归因报告 ${String(row.report_date || '-')}`,
    '数据来源：远端 strategy_attribution_reports（Web 不读取本地 --no-write 报告）',
    `窗口：${String(row.window_start || '-')} 至 ${String(row.window_end || '-')}`,
    attributionGovernorLine(governor),
    `下一步：${String(governor.next_action_summary || '-')}`,
    `治理摘要：${String(governor.summary || '-')}`,
    promotionChecklistLine(governor.promotion_checklist),
    attributionExecutionLine(execution),
    `操作摘要：${buildAttributionOperatorSummary({ operations, execution, latest, actions })}`,
    latestShadowLine(latest),
    sampleLine('Shadow 新增样本', latest.diff_added_sample),
    sampleLine('Shadow 移除样本', latest.diff_removed_sample),
    actionLines(actions),
  ].filter(Boolean).join('\n')
}

function attributionExecutionFallback(governor: Record<string, unknown>, rawActions: unknown): Record<string, unknown> {
  const horizon = String(governor.horizon || '5')
  const actionCount = jsonArray(rawActions).filter(row => isSignalAction(row) && String(row.horizon || payloadOf(row).horizon || '') === horizon).length
  const formal = fallbackFormalDynamic(governor)
  const formalBlockReason = formal.allowed ? 'execution_state=missing' : formal.reason
  return withAttributionActiveScope({
    funnel_dynamic_policy: 'unknown',
    horizon,
    scope: actionCount > 0 ? 'funnel_shadow' : 'none',
    signal_action_count: actionCount,
    promotion_status: String(governor.promotion_status || 'unknown'),
    next_action: String(governor.next_action || 'keep_shadow_observe'),
    next_action_summary: String(governor.next_action_summary || '-'),
    formal_dynamic_allowed: false,
    formal_dynamic_block_reason: formalBlockReason,
    promotion_checklist: Array.isArray(governor.promotion_checklist) ? governor.promotion_checklist : [],
    summary: actionCount > 0 ? `h=${horizon} 有 ${actionCount} 个信号级调权；缺少后端执行态，默认只按 shadow 展示。` : '暂无可执行信号调权。',
  })
}

function attributionExecutionLine(execution: Record<string, unknown>): string {
  return [
    `执行态：${executionModeText(execution.funnel_dynamic_policy)}`,
    `周期=h${String(execution.horizon || '5')}`,
    `作用范围=${executionScopeText(execution)}`,
    `晋级=${attributionPromotionStatusLabel(execution.promotion_status)}`,
    `下一步=${attributionNextActionLabel(execution.next_action)}`,
    `正式dynamic=${formalDynamicText(execution)}`,
    `可执行调权=${Number(execution.signal_action_count || 0)}`,
    String(execution.summary || ''),
  ].filter(Boolean).join(' | ')
}

function executionModeText(raw: unknown): string {
  const value = String(raw || 'unknown').trim()
  const labels: Record<string, string> = {
    on: '正式调权(on)',
    shadow: 'shadow 对照(shadow)',
    off: '静态策略(off)',
    unknown: '未知模式',
  }
  return labels[value] || `${value} 模式`
}

function executionScopeText(execution: Record<string, unknown>): string {
  const active = String(execution.active_scope || '无')
  const scope = String(execution.scope || 'none').trim()
  return scope && scope !== 'none' ? `${active}（底层=${scope}）` : active
}

function attributionGovernorLine(governor: Record<string, unknown>): string {
  return [
    `策略治理：状态=${attributionGovernorStatusLabel(governor.status)}`,
    `建议=${attributionModeRecommendationLabel(governor.mode_recommendation)}`,
    `下一步=${attributionNextActionLabel(governor.next_action)}`,
    `晋级=${attributionPromotionStatusLabel(governor.promotion_status)}`,
    `自动生效=${Boolean(governor.auto_apply) ? '是' : '否'}`,
  ].join(' ')
}

function withAttributionActiveScope(execution: Record<string, unknown>): Record<string, unknown> {
  const flags = attributionActiveFlags(execution)
  return {
    ...execution,
    active_scope: String(execution.active_scope || flags.active_scope),
    funnel_shadow_weights_active: execution.funnel_shadow_weights_active ?? flags.funnel_shadow_weights_active,
    funnel_formal_weights_active: execution.funnel_formal_weights_active ?? flags.funnel_formal_weights_active,
  }
}

function attributionActiveFlags(execution: Record<string, unknown>): Record<string, unknown> {
  const actionCount = Number(execution.signal_action_count || 0)
  const scope = String(execution.scope || 'none').trim()
  const shadowActive = actionCount > 0 && scope === 'funnel_shadow'
  const formalActive = actionCount > 0 && scope === 'funnel_formal'
  const labels = []
  if (formalActive) labels.push('正式漏斗')
  else if (shadowActive) labels.push('漏斗shadow')
  return {
    active_scope: labels.join('+') || '无',
    funnel_shadow_weights_active: shadowActive,
    funnel_formal_weights_active: formalActive,
  }
}

function formalDynamicText(execution: Record<string, unknown>): string {
  return attributionFormalDynamicLabel(execution)
}

function fallbackFormalDynamic(governor: Record<string, unknown>): { allowed: boolean, reason: string } {
  const explicit = truthValue(governor.formal_dynamic_allowed)
  if (explicit === true) {
    const checklistBlock = promotionChecklistBlockReason(governor.promotion_checklist)
    return checklistBlock ? { allowed: false, reason: checklistBlock } : { allowed: true, reason: '' }
  }
  if (explicit === false) {
    return { allowed: false, reason: String(governor.formal_dynamic_block_reason || 'formal_dynamic_allowed=false') }
  }
  if (String(governor.next_action || '').trim() === 'manual_review_dynamic_on') {
    return { allowed: false, reason: 'manual_review_required' }
  }
  if (String(governor.next_action || '').trim() === 'formal_dynamic_approved') {
    const checklistBlock = promotionChecklistBlockReason(governor.promotion_checklist)
    return checklistBlock ? { allowed: false, reason: checklistBlock } : { allowed: true, reason: '' }
  }
  if (String(governor.next_action || '').trim() === 'review_policy_actions') {
    return { allowed: false, reason: promotionChecklistBlockReason(governor.promotion_checklist) || 'signal_actions_review_required' }
  }
  if (String(governor.next_action || '').trim() === 'run_backtest_confirmation') {
    return { allowed: false, reason: 'backtest_confirmation_required' }
  }
  if (String(governor.next_action || '').trim() === 'keep_shadow_backtest_failed') {
    return { allowed: false, reason: 'backtest_confirmation_failed' }
  }
  return { allowed: false, reason: String(governor.next_action || 'unknown') }
}

function promotionChecklistBlockReason(raw: unknown): string {
  const rows = arrayValues(raw).filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
  if (rows.length === 0) return 'promotion_checklist=missing'
  const blocked = rows
    .map((row) => {
      const status = String(row.status || '').trim().toLowerCase()
      return ['pass', 'not_required'].includes(status) ? '' : `${String(row.key || 'unknown')}:${status || 'unknown'}`
    })
    .filter(Boolean)
  return blocked.length ? `promotion_checklist=${blocked.join(',')}` : ''
}

function truthValue(value: unknown): boolean | null {
  if (value === true || value === false) return value
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase()
    if (normalized === 'true') return true
    if (normalized === 'false') return false
  }
  return null
}

function promotionChecklistLine(raw: unknown): string {
  const rows = arrayValues(raw).filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
  if (rows.length === 0) return '晋级检查：暂无'
  return `晋级检查：${rows.map((row) => `${checklistKeyLabel(row.key)}:${checklistStatusLabel(row.status)}`).join('；')}`
}

function latestShadowLine(latest: Record<string, unknown>): string {
  const selection = jsonMapOrNull(latest.selection_summary) || {}
  if (!latest.trade_date && Object.keys(selection).length === 0) return '最新 Shadow：暂无'
  return [
    `最新 Shadow：${String(latest.trade_date || '-')} / ${String(latest.regime || '-')}`,
    `base=${fmtUnknown(selection.base_count)}`,
    `shadow=${fmtUnknown(selection.shadow_count)}`,
    `新增=${fmtUnknown(selection.diff_added_count)}`,
    `移除=${fmtUnknown(selection.diff_removed_count)}`,
    `Jaccard=${fmtUnknown(selection.jaccard)}`,
  ].join(' | ')
}

function sampleLine(label: string, raw: unknown): string {
  const sample = arrayValues(raw).map(String).filter(Boolean).slice(0, 12)
  return `${label}：${sample.length > 0 ? sample.join(', ') : '-'}`
}

function actionLines(actions: Record<string, unknown>[]): string {
  if (actions.length === 0) return '调权明细：无'
  const lines = actions.map(actionLine)
  return `调权明细：\n${lines.join('\n')}`
}

function actionLine(row: Record<string, unknown>): string {
  const payload = payloadOf(row)
  const scope = jsonMapOrNull(payload.scope) || {}
  const target = String(row.target || payload.target || '-')
  const label = scopedSignalLabel(target, scope)
  const evidence = jsonMapOrNull(payload.evidence) || {}
  return [
    `- ${label}`,
    String(row.type || payload.action || '-'),
    `h=${String(row.horizon || payload.horizon || '-')}`,
    `x${fmtWeight(payload.weight_multiplier)}`,
    `avg=${fmtUnknown(evidence.avg_return_pct)}`,
    `win=${fmtUnknown(evidence.win_rate_pct)}%`,
    `dd=${fmtUnknown(evidence.avg_drawdown_pct)}`,
  ].join(' | ')
}

function scopedSignalLabel(signal: string, scope: Record<string, unknown>): string {
  const parts = [
    scope.regime ? `regime=${String(scope.regime)}` : '',
    scope.lane ? `lane=${String(scope.lane)}` : '',
    scope.entry_type || scope.entry ? `entry=${String(scope.entry_type || scope.entry)}` : '',
  ].filter(Boolean)
  return parts.length > 0 ? `${signal}[${parts.join(', ')}]` : signal
}

function jsonArray(raw: unknown): Record<string, unknown>[] {
  const value = typeof raw === 'string' ? parseJson(raw) : raw
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : []
}

function arrayValues(raw: unknown): unknown[] {
  const value = typeof raw === 'string' ? parseJson(raw) : raw
  return Array.isArray(value) ? value : []
}

function isSignalAction(row: Record<string, unknown>): boolean {
  const action = String(row.type || payloadOf(row).action || '')
  return action !== '' && action !== 'policy_governor'
}

function payloadOf(row: Record<string, unknown>): Record<string, unknown> {
  return jsonMapOrNull(row.reason) || {}
}

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw) as unknown
  } catch {
    return null
  }
}

function fmtWeight(raw: unknown): string {
  const value = Number(raw ?? 1)
  return Number.isFinite(value) ? value.toFixed(2) : '1.00'
}

function fmtUnknown(raw: unknown): string {
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw.toFixed(2).replace(/\.00$/, '')
  const text = String(raw ?? '').trim()
  return text || '-'
}

export async function execExecutePortfolioUpdate(
  deps: ToolDeps,
  userId: string,
  action: 'add' | 'update' | 'delete',
  code: string,
  name: string | null,
  shares: number | null,
  cost_price: number | null,
  stop_loss: number | null,
): Promise<string> {
  const portfolioId = `USER_LIVE:${userId}`

  if (action === 'delete') {
    const { error } = await deps.supabase
      .from('portfolio_positions')
      .delete()
      .eq('portfolio_id', portfolioId)
      .eq('code', code)
    return error ? `删除失败: ${error.message}` : `✅ 已删除 ${code} ${name || ''}`
  }

  if (action === 'add' || action === 'update') {
    if (!name || !shares || !cost_price) {
      return '执行失败：缺少 name、shares、cost_price 参数'
    }
    const record: Record<string, unknown> = {
      portfolio_id: portfolioId, code, name, shares, cost_price,
      buy_dt: new Date().toISOString().slice(0, 10),
    }
    if (stop_loss !== undefined) record.stop_loss = stop_loss
    const error = await savePortfolioPosition(deps, portfolioId, code, record)
    return error
      ? `执行失败: ${error}`
      : `✅ 已${action === 'add' ? '新增' : '更新'} ${code} ${name} ${shares}股 @¥${cost_price}${stop_loss ? ` 止损¥${stop_loss}` : ''}`
  }

  return '未知操作'
}

async function savePortfolioPosition(
  deps: ToolDeps,
  portfolioId: string,
  code: string,
  record: Record<string, unknown>,
): Promise<string | null> {
  const { data, error } = await deps.supabase
    .from('portfolio_positions')
    .update(record)
    .eq('portfolio_id', portfolioId)
    .eq('code', code)
    .select('id')
  if (error) return error.message
  if (Array.isArray(data) && data.length > 0) return null

  const { error: insertError } = await deps.supabase.from('portfolio_positions').insert(record)
  return insertError?.message || null
}

export interface ScreenStockItem {
  code: string
  name: string
  funnel_score: number | null
  change_pct: number | null
  candidate_lane: string | null
  candidate_label: string | null
  entry_type: string | null
}

export interface ScreenResult {
  date: string
  stocks: ScreenStockItem[]
  meta: { ai_count: number }
  strategy_policy?: ScreenStrategyPolicy | null
}

export const SCREEN_RESULT_OUTPUT_SCHEMA = z.object({
  date: z.string(),
  stocks: z.array(z.object({
    code: z.string(),
    name: z.string(),
    funnel_score: z.number().nullable(),
    change_pct: z.number().nullable(),
    candidate_lane: z.string().nullable(),
    candidate_label: z.string().nullable(),
    entry_type: z.string().nullable(),
  })),
  meta: z.object({ ai_count: z.number() }),
  strategy_policy: STRATEGY_POLICY_OUTPUT_SCHEMA.nullable().optional(),
})

export async function execScreenStocks(deps: ToolDeps): Promise<ScreenResult> {
  const { data } = await deps.supabase
    .from('recommendation_tracking')
    .select('code, name, recommend_date, funnel_score, change_pct, is_ai_recommended, candidate_lane, entry_type')
    .eq('is_ai_recommended', true)
    .order('recommend_date', { ascending: false })
    .limit(30)

  if (!data || data.length === 0) return { date: '', stocks: [], meta: { ai_count: 0 } }

  const latestDate = data[0]!.recommend_date
  const latest = data.filter(r => r.recommend_date === latestDate)
  const strategyPolicy = await fetchLatestStrategyPolicy(deps)

  const result: ScreenResult = {
    date: latestDate,
    stocks: latest.map(r => ({
      code: normalizeCode(r.code),
      name: r.name,
      funnel_score: r.funnel_score ?? null,
      change_pct: r.change_pct ?? null,
      candidate_lane: r.candidate_lane ?? null,
      candidate_label: labelCandidateTerm(r.candidate_lane ?? r.entry_type ?? ''),
      entry_type: r.entry_type ?? null,
    })),
    meta: { ai_count: latest.length },
  }
  if (strategyPolicy) result.strategy_policy = strategyPolicy

  return result
}

async function fetchLatestStrategyPolicy(deps: ToolDeps): Promise<ScreenStrategyPolicy | null> {
  try {
    const { data } = await deps.supabase
      .from('signal_policy_shadow_runs')
      .select('trade_date,market,shadow_diff_stats_json,recommendations_json')
      .eq('market', 'cn')
      .order('trade_date', { ascending: false })
      .limit(1)
    return strategyPolicyFromRow(Array.isArray(data) ? data[0] : null)
  } catch {
    return null
  }
}

function strategyPolicyFromRow(row: unknown): ScreenStrategyPolicy | null {
  const item = recordValue(row)
  const shadow = recordValue(item?.shadow_diff_stats_json)
  const operations = recordValue(shadow?.policy_operations_brief)
  const execution = recordValue(shadow?.policy_execution_state)
  const governor = recordValue(shadow?.policy_governor)
  const summary = stringValue(operations?.selection_action_summary)
  const weights = policyWeightsFromRows(Array.isArray(item?.recommendations_json) ? item.recommendations_json : [], stringValue(governor?.horizon))
  if (!summary && Object.keys(weights).length === 0 && !stringValue(operations?.active_scope)) return null
  return {
    dynamic_mode: stringValue(execution?.funnel_dynamic_policy) || null,
    execution_policy: stringValue(execution?.funnel_dynamic_policy) || null,
    policy_weight_active_scope: stringValue(operations?.active_scope || execution?.active_scope) || null,
    selection_action_count: numberValue(operations?.selection_action_count),
    selection_action_summary: summary || null,
    formal_dynamic_allowed: booleanValue(operations?.formal_dynamic_allowed ?? execution?.formal_dynamic_allowed),
    next_action: stringValue(operations?.next_action || execution?.next_action || governor?.next_action) || null,
    signal_weights: Object.keys(weights).length ? weights : null,
    attribution_signal_weights: Object.keys(weights).length ? weights : null,
  }
}

function policyWeightsFromRows(rows: unknown[], horizon: string): Record<string, number> {
  const weights: Record<string, number> = {}
  for (const row of rows) {
    const item = recordValue(row)
    if (!item || (horizon && stringValue(item.horizon) !== horizon)) continue
    const payload = jsonRecord(item.reason)
    const target = stringValue(payload?.target ?? item.target)
    const multiplier = numberValue(payload?.weight_multiplier ?? item.weight_multiplier)
    if (target && multiplier != null) weights[target] = multiplier
  }
  return weights
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function jsonRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string' || !value.trim()) return null
  try {
    return recordValue(JSON.parse(value))
  } catch {
    return null
  }
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
}

export async function execAnalyzeStock(
  deps: ToolDeps, userId: string, _config: LLMToolConfig, model: unknown, code: string, name: string | null,
): Promise<AnalyzeStockResult> {
  const keys = await fetchUserDataKeys(deps, userId)
  if (!isCnSymbol(code) && !keys.tickflow) {
    return buildAnalyzeError(code, name, `无法获取 ${code} ${name || ''} 的K线数据。美股/港股诊断需要先在设置页配置 TickFlow API Key，并使用标准代码（如 AAPL.US / 00700.HK）。`)
  }
  const [klineResult, valueSnapshot] = await Promise.all([
    fetchKlineForAgentWithQuality(deps, code, keys, userId),
    fetchValueSnapshotForAgent(deps, code, keys).catch((): ValueSnapshot => ({ symbol: code, source: 'none', metrics: null, reason: 'not-found' })),
  ])
  const { rows: kline, quality } = klineResult
  if (kline.length === 0) {
    return buildAnalyzeError(code, name, `无法获取 ${code} ${name || ''} 的K线数据。美股/港股请使用 TickFlow 标准代码（如 AAPL.US / 00700.HK）。推荐购买 TickFlow 获取实时行情：https://tickflow.org/auth/register?ref=5N4NKTCPL4`)
  }

  const digest = [
    `数据来源：${quality.source === 'tickflow' ? 'TickFlow' : quality.source === 'tushare' ? 'Tushare' : quality.source}`,
    `数据覆盖：${quality.coverageStart || '未知'} 至 ${quality.coverageEnd || '未知'}；最新交易日：${quality.latestTradingDate || '未知'}；返回 ${quality.returnedRows}/${quality.requestedRows} 根${quality.fallbackUsed ? '；已发生数据源回退' : ''}`,
    buildKlineDigest(kline),
  ].join('\n')
  const valueDigest = buildValueAgentDigest(valueSnapshot)
  const contextPack = buildStockAnalysisContextPack({ symbol: code, name, kline, dataQuality: quality, valueSnapshot })
  const systemPrompt = `你是威科夫分析大师。基于以下K线数据和价值面摘要，对 ${code} ${name || ''} 进行深度诊断。主框架仍是量价与威科夫阶段判断，价值面只作为质量、风险和仓位置信度校准：技术面负责时机，价值面负责是否值得提高/降低结论置信度。
1. 当前威科夫阶段（积累/上涨/派发/下跌），Phase A-E 定位
2. 量价关系分析（供需力量对比，近期量比变化）
3. 均线形态（多头/空头排列，金叉/死叉）
4. 关键支撑与阻力位
5. 价值面校准（盈利质量、成长、杠杆、现金流如何影响置信度）
6. 主力行为判断（是否有吸筹/出货迹象）
7. 操作建议与风险提示（含建议止损位）

按结构化 schema 输出。markdown 字段保留一段简洁专业的 Markdown 诊断正文。`
  const userPrompt = `${valueDigest}\n\n${digest}`
  try {
    const result = await deps.generateText({
      model: model as Parameters<typeof GenerateTextFn>[0]['model'],
      system: systemPrompt,
      prompt: userPrompt,
      output: Output.object({ schema: ANALYZE_STOCK_OUTPUT_SCHEMA }),
    })
    return withAnalyzeQuality(normalizeAnalyzeOutput(result.output, result.text), quality, contextPack)
  } catch {
    const fallback = await deps.generateText({
      model: model as Parameters<typeof GenerateTextFn>[0]['model'],
      system: systemPrompt + '\n\n请用纯 JSON 输出，字段: summary, phase, confidence, support, resistance, action, risk, markdown。',
      prompt: userPrompt,
    })
    return withAnalyzeQuality(parseAnalyzeFallback(fallback.text, code, name), quality, contextPack)
  }
}

function buildAnalyzeError(code: string, name: string | null, message: string): AnalyzeStockResult {
  return {
    summary: message,
    phase: '数据不足',
    confidence: null,
    support: null,
    resistance: null,
    action: '暂不判断',
    risk: '数据源不可用，不能据此交易。',
    markdown: `## ${code} ${name || ''}\n${message}`,
    data_source: 'none',
    data_asof: null,
    data_quality: buildKlineDataQuality('none', 320, []),
  }
}

function withAnalyzeQuality(result: AnalyzeStockResult, quality: KlineDataQuality, contextPack?: AnalyzeStockResult['context_pack']): AnalyzeStockResult {
  return {
    ...result,
    data_source: quality.source,
    data_asof: quality.latestTradingDate,
    data_quality: quality,
    context_pack: contextPack,
  }
}

function normalizeAnalyzeOutput(output: AnalyzeStockResult | undefined, text: string): AnalyzeStockResult {
  if (output) return output
  return parseAnalyzeFallback(text, '', null)
}

function parseAnalyzeFallback(text: string, code: string, name: string | null): AnalyzeStockResult {
  const raw = text || ''
  const jsonMatch = raw.match(/\{[\s\S]*\}/)
  if (jsonMatch) {
    try {
      const parsed = JSON.parse(jsonMatch[0])
      return {
        summary: String(parsed.summary || parsed.markdown || raw).slice(0, 2000),
        phase: String(parsed.phase || '未知'),
        confidence: typeof parsed.confidence === 'number' ? parsed.confidence : null,
        support: parsed.support != null ? String(parsed.support) : null,
        resistance: parsed.resistance != null ? String(parsed.resistance) : null,
        action: String(parsed.action || '详见正文'),
        risk: String(parsed.risk || '请结合实时行情与自身风险承受能力。'),
        markdown: String(parsed.markdown || parsed.summary || raw),
      }
    } catch { /* fall through */ }
  }
  const label = [code, name].filter(Boolean).join(' ')
  return {
    summary: raw || '分析完成但无结构化输出',
    phase: '未结构化',
    confidence: null,
    support: null,
    resistance: null,
    action: '详见正文',
    risk: '请结合实时行情与自身风险承受能力。',
    markdown: label ? `## ${label}\n${raw}` : raw || '分析完成但无输出',
  }
}

export async function execGenerateAiReport(
  deps: ToolDeps, userId: string, _config: LLMToolConfig, model: unknown, codes: string[],
): Promise<string> {
  const [keys, strategyPolicy] = await Promise.all([
    fetchUserDataKeys(deps, userId),
    fetchLatestStrategyPolicy(deps),
  ])
  const policyDigest = formatStrategyPolicyDigest(strategyPolicy)

  const results: string[] = []
  for (const code of codes.slice(0, 3)) {
    const [kline, valueSnapshot] = await Promise.all([
      fetchKlineForAgent(deps, code, keys, userId),
      fetchValueSnapshotForAgent(deps, code, keys).catch((): ValueSnapshot => ({ symbol: code, source: 'none', metrics: null, reason: 'not-found' })),
    ])
    if (kline.length === 0) {
      results.push(`## ${code}\n无法获取K线数据。美股/港股请使用 TickFlow 标准代码（如 AAPL.US / 00700.HK）。\n`)
      continue
    }
    const digest = buildKlineDigest(kline)
    const valueDigest = buildValueAgentDigest(valueSnapshot)
    const result = await deps.generateText({
      model: model as Parameters<typeof GenerateTextFn>[0]['model'],
      system: `你是威科夫分析大师。为 ${code} 撰写一份简明研报，包含：阶段判断、量价特征、价值面校准、关键价位、操作建议、当前策略治理影响。价值面只校准质量/风险/置信度，不替代技术面。250字以内。`,
      prompt: `策略治理:\n${policyDigest}\n\n${valueDigest}\n\n${digest}`,
    })
    results.push(`## ${code}\n${result.text || '无输出'}\n`)
  }

  const report = results.join('\n---\n\n')
  return strategyPolicy ? `### 策略治理\n${policyDigest}\n\n---\n\n${report}` : report
}

export async function execStrategyDecision(deps: ToolDeps, userId: string, model: unknown): Promise<StrategyDecisionResult> {
  const portfolioId = `USER_LIVE:${userId}`

  const [posResult, signalResult, strategyPolicy] = await Promise.all([
    deps.supabase.from('portfolio_positions').select('code, name, shares, cost_price, stop_loss').eq('portfolio_id', portfolioId),
    deps.supabase.from('market_signal_daily').select('*').order('trade_date', { ascending: false }).limit(1).single(),
    fetchLatestStrategyPolicy(deps),
  ])

  const positions = posResult.data || []
  const signal = signalResult.data

  if (positions.length === 0) {
    return {
      summary: '当前无持仓，无法给出操作建议。建议先通过选股工具寻找标的。',
      market_regime: signal?.benchmark_regime || '未知',
      overall_position: '空仓',
      risk: '没有持仓数据，不能生成个股级调仓建议。',
      position_actions: [],
      strategy_policy: strategyPolicy,
    }
  }

  const posInfo = positions.map(p =>
    `${p.code} ${p.name} | ${p.shares}股 成本¥${p.cost_price}${p.stop_loss ? ` 止损¥${p.stop_loss}` : ''}`
  ).join('\n')

  const marketInfo = signal
    ? `大盘状态: ${signal.benchmark_regime || '未知'}, 上证: ${signal.main_index_close || '--'}, A50涨幅: ${signal.a50_pct_chg || '--'}%, VIX: ${signal.vix_close || '--'}`
    : '暂无市场数据'
  const policyInfo = formatStrategyPolicyDigest(strategyPolicy)
  const sysPrompt = '你是威科夫大师。基于用户的持仓和当前市场环境，为每只持仓股给出操作建议（买入加仓/持有/减仓/卖出），并给出整体仓位管理建议。按结构化 schema 输出，必须附带风险提示。'
  const userPrompt = `当前持仓:\n${posInfo}\n\n市场环境:\n${marketInfo}\n\n策略治理:\n${policyInfo}`
  const defaultRegime = signal?.benchmark_regime || '未知'
  try {
    const result = await deps.generateText({
      model: model as Parameters<typeof GenerateTextFn>[0]['model'],
      system: sysPrompt,
      prompt: userPrompt,
      output: Output.object({ schema: STRATEGY_DECISION_OUTPUT_SCHEMA }),
    })
    return withStrategyPolicy(result.output || strategyDecisionFallback(result.text, defaultRegime), strategyPolicy)
  } catch {
    const fallback = await deps.generateText({
      model: model as Parameters<typeof GenerateTextFn>[0]['model'],
      system: sysPrompt + '\n\n请用纯 JSON 输出，字段: summary, market_regime, overall_position, risk, position_actions。',
      prompt: userPrompt,
    })
    return withStrategyPolicy(strategyDecisionFallback(fallback.text, defaultRegime), strategyPolicy)
  }
}

function strategyDecisionFallback(text: string, regime: string): StrategyDecisionResult {
  const raw = text || ''
  const jsonMatch = raw.match(/\{[\s\S]*\}/)
  if (jsonMatch) {
    try {
      const parsed = JSON.parse(jsonMatch[0])
      return {
        summary: String(parsed.summary || raw),
        market_regime: String(parsed.market_regime || regime),
        overall_position: String(parsed.overall_position || '详见摘要'),
        risk: String(parsed.risk || '请结合实时行情与自身风险承受能力。'),
        position_actions: Array.isArray(parsed.position_actions) ? parsed.position_actions : [],
      }
    } catch { /* fall through */ }
  }
  return {
    summary: raw || '无法生成建议',
    market_regime: regime,
    overall_position: '详见摘要',
    risk: '请结合实时行情与自身风险承受能力。',
    position_actions: [],
  }
}

function withStrategyPolicy(result: StrategyDecisionResult, policy: ScreenStrategyPolicy | null): StrategyDecisionResult {
  return policy ? { ...result, strategy_policy: policy } : result
}

function formatStrategyPolicyDigest(policy: ScreenStrategyPolicy | null): string {
  if (!policy) return '无最新策略治理摘要。'
  const lines = [
    `执行策略: ${executionModeText(policy.execution_policy || policy.dynamic_mode || 'unknown')}`,
    `生效范围: ${policy.policy_weight_active_scope || '未知'}`,
    `下一步: ${attributionNextActionLabel(policy.next_action)}`,
    `候选源治理: ${policy.selection_action_summary || '无'}`,
  ]
  const weights = policy.attribution_signal_weights || policy.signal_weights
  if (weights && Object.keys(weights).length > 0) {
    lines.push(`归因调权: ${Object.entries(weights).map(([key, value]) => `${key}×${value.toFixed(2)}`).join('，')}`)
  }
  return lines.join('\n')
}

export async function execIntradayAnalysis(deps: ToolDeps, userId: string, code: string): Promise<string> {
  const apiKey = await fetchTickFlowKey(deps, userId)
  if (!apiKey) return '未配置 TickFlow API Key，无法获取分钟线数据。请在设置中配置。'
  const symbol = normalizeTickFlowSymbol(code)
  const periods = ['1m', '5m', '15m'] as const
  const results = await Promise.all(periods.map(async (period) => {
    const params = new URLSearchParams({ symbol, period, count: period === '1m' ? '500' : '100' })
    const resp = await deps.fetch(`/api/llm-proxy/v1/klines/intraday?${params}`, {
      headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
    })
    if (!resp.ok) return []
    return parseTickFlowPayload(await resp.json(), symbol)
  }))
  const [rows1m, rows5m, rows15m] = results
  if (!rows1m || rows1m.length < 10) return `${code} 无法获取分钟线数据，可能非交易时段或代码有误。`
  const profile = computeIntradayProfile(rows1m, rows5m || [], rows15m || [])
  const lines = [
    `📊 ${code} 盘中简评（${rows1m.length}根1m线，仅供参考，权威评分以后端策略为准）`,
    `VWAP位置: ${profile.vwapPos > 0 ? '上方' : '下方'} ${profile.vwapPos.toFixed(2)}%`,
    `日内位置: ${(profile.closePos * 100).toFixed(0)}%（0=最低 100=最高）`,
    `5m趋势: ${profile.trendShort} | 15m趋势: ${profile.trendMid}`,
    `30m动量: ${profile.momentum30m.toFixed(2)}% | 15m动量: ${profile.momentum15m.toFixed(2)}%`,
    `量能分布: ${profile.volumeConcentration}`,
    `参考强度: ${profile.strengthScore.toFixed(0)}/100（简化算法，不含量价深度分析）`,
  ]
  return lines.join('\n')
}

interface IntradayProfileWeb {
  vwapPos: number; closePos: number
  trendShort: string; trendMid: string
  momentum30m: number; momentum15m: number
  volumeConcentration: string; strengthScore: number
}

function computeIntradayProfile(rows1m: KlineRow[], rows5m: KlineRow[], rows15m: KlineRow[]): IntradayProfileWeb {
  const closes1m = rows1m.map(r => r.close)
  const volumes1m = rows1m.map(r => r.volume)
  const highs1m = rows1m.map(r => r.high || r.close)
  const lows1m = rows1m.map(r => r.low || r.close)
  const last = closes1m[closes1m.length - 1]!
  const dayHigh = Math.max(...highs1m)
  const dayLow = Math.min(...lows1m)
  const dayRange = Math.max(dayHigh - dayLow, 1e-8)
  const closePos = Math.max(0, Math.min(1, (last - dayLow) / dayRange))
  const totalAmount = rows1m.reduce((s, r) => s + r.close * r.volume, 0)
  const totalVol = volumes1m.reduce((s, v) => s + v, 0)
  const vwap = totalVol > 0 ? totalAmount / totalVol : last
  const vwapPos = vwap > 0 ? (last / vwap - 1) * 100 : 0
  const momentum30m = retPct(closes1m, 30)
  const momentum15m = retPct(closes1m, 15)
  const trendShort = rows5m.length >= 4 ? computeTrendDir(rows5m) : computeTrendDir(rows1m)
  const trendMid = rows15m.length >= 4 ? computeTrendDir(rows15m) : 'flat'
  const mid = (dayHigh + dayLow) / 2
  const volAbove = rows1m.filter(r => r.close >= mid).reduce((s, r) => s + r.volume, 0)
  const volTotal = totalVol || 1
  const ratio = volAbove / volTotal
  const volumeConcentration = ratio > 0.62 ? '堆量在高位' : ratio < 0.38 ? '堆量在低位' : '均匀分布'
  const strengthScore = computeStrength(vwapPos, closePos, momentum30m, momentum15m, trendShort, trendMid, volumeConcentration)
  return { vwapPos, closePos, trendShort, trendMid, momentum30m, momentum15m, volumeConcentration, strengthScore }
}

function retPct(closes: number[], lookback: number): number {
  if (closes.length <= lookback) return 0
  const base = closes[closes.length - 1 - lookback]!
  const now = closes[closes.length - 1]!
  return base > 0 ? (now / base - 1) * 100 : 0
}

function computeTrendDir(rows: KlineRow[]): string {
  if (rows.length < 4) return 'flat'
  const closes = rows.slice(-8).map(r => r.close)
  const n = closes.length
  const xMean = (n - 1) / 2
  const yMean = closes.reduce((a, b) => a + b, 0) / n
  let num = 0, den = 0
  for (let i = 0; i < n; i++) { num += (i - xMean) * (closes[i]! - yMean); den += (i - xMean) ** 2 }
  const slope = den > 0 ? num / den : 0
  const pctSlope = (slope / (yMean || 1)) * 100
  if (pctSlope > 0.03) return 'up'
  if (pctSlope < -0.03) return 'down'
  return 'flat'
}

function computeStrength(vwap: number, closePos: number, m30: number, m15: number, ts: string, tm: string, vc: string): number {
  let s = 50
  s += vwap >= 0.8 ? 12 : vwap >= 0 ? 5 : -8
  s += closePos >= 0.8 ? 10 : closePos >= 0.6 ? 4 : closePos < 0.35 ? -10 : 0
  s += m30 >= 0.8 ? 8 : m30 >= 0.3 ? 3 : m30 <= -0.8 ? -8 : 0
  s += m15 <= -0.5 ? -5 : m15 >= 0.4 ? 3 : 0
  s += vc === '堆量在高位' ? 5 : vc === '堆量在低位' ? -5 : 0
  s += ts === 'up' ? 4 : ts === 'down' ? -4 : 0
  s += tm === 'up' ? 3 : tm === 'down' ? -3 : 0
  return Math.max(0, Math.min(100, s))
}

function todayDateString(): string {
  return new Date(Date.now() + 8 * 3600_000).toISOString().slice(0, 10)
}

const FRESHNESS_FALLBACK: Record<string, string> = {
  尾盘记录: '建议改用 screen_stocks 获取最新候选池，对候选标的调用 intraday_analysis 获取实时盘中数据做尾盘判断。',
  市场信号: '建议调用 market_history 获取实时大盘K线做判断。',
  策略归因: '建议直接调用 query_attribution 读取远端报告，或用 screen_stocks 查看候选池。',
}

function dataFreshnessNote(dataDate: string, label: string): string {
  const today = todayDateString()
  const dStr = String(dataDate || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3').slice(0, 10)
  if (!dStr || dStr >= today) return ''
  const diffMs = new Date(today).getTime() - new Date(dStr).getTime()
  const diffDays = Math.round(diffMs / 86_400_000)
  if (diffDays <= 3) return ''
  const fallback = FRESHNESS_FALLBACK[label] || ''
  return `⚠️ ${label}最新数据日期为 ${dStr}，距今已 ${diffDays} 天，数据可能已过期。${fallback}`.trim()
}
