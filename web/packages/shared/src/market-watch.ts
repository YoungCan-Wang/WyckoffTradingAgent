import { normalizeCode, normalizeTickFlowSymbol } from './agent-market'

export type MarketWatchState = 'ready' | 'unavailable' | 'empty'
export const MARKET_WATCH_CACHE_TTL_MS = 45_000

export interface MarketWatchQuote {
  requestedCode: string
  symbol: string
  price: number | null
  changePct: number | null
  previousClose: number | null
  volume: number | null
  asOf: string | null
}

export interface MarketWatchSnapshot {
  state: MarketWatchState
  source: 'tickflow' | 'none'
  requestedCodes: string[]
  quotes: MarketWatchQuote[]
  fetchedAt: string
  fromCache: boolean
  message?: string
}

export function normalizeMarketWatchCode(code: string): string {
  return normalizeCode(code)
}

export function marketWatchSymbol(code: string): string {
  const normalized = normalizeMarketWatchCode(code)
  return /^\d{6}$/.test(normalized) ? normalizeTickFlowSymbol(normalized) : normalized
}

export function selectMarketWatchCodes(
  requestedItems: Array<string | { code: string; name?: string | null }>,
  query: string,
): string[] {
  const items = requestedItems
    .map((item) => typeof item === 'string' ? { code: item, name: '' } : item)
    .map((item) => ({
      code: normalizeMarketWatchCode(item.code),
      name: String(item.name || '').trim(),
    }))
    .filter((item) => item.code)
  const codes = Array.from(new Set(items.map((item) => item.code)))
  if (codes.length === 0) return []
  const normalizedQuery = query.toUpperCase()
  const mentioned = items.filter((item) => {
    const code = item.code
    const baseCode = code.split('.')[0] || code
    const name = item.name.toUpperCase()
    return normalizedQuery.includes(code)
      || (baseCode.length >= 5 && normalizedQuery.includes(baseCode))
      || (name.length >= 2 && normalizedQuery.includes(name))
  }).map((item) => item.code)
  if (mentioned.length > 0) return Array.from(new Set(mentioned))
  return /观察篮|观察清单|自选|复盘|读盘|盘前|盘中|尾盘|持仓|候选/.test(query) ? codes : []
}

export function readFreshMarketWatchSnapshot(
  value: unknown,
  requestedCodes: string[],
  now = Date.now(),
): MarketWatchSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const expectedCodes = normalizeCodes(requestedCodes)
  const cachedCodes = normalizeCodes(Array.isArray(raw.requestedCodes) ? raw.requestedCodes.filter((code): code is string => typeof code === 'string') : [])
  if (expectedCodes.length === 0 || !sameCodes(expectedCodes, cachedCodes)) return null
  if (raw.state !== 'ready' || raw.source !== 'tickflow' || typeof raw.fetchedAt !== 'string') return null
  const fetchedAtMs = Date.parse(raw.fetchedAt)
  if (!Number.isFinite(fetchedAtMs) || now - fetchedAtMs < -5_000 || now - fetchedAtMs > MARKET_WATCH_CACHE_TTL_MS) return null
  if (!Array.isArray(raw.quotes) || raw.quotes.length !== expectedCodes.length) return null

  const quotes = raw.quotes.map((value): MarketWatchQuote | null => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const quote = value as Record<string, unknown>
    const requestedCode = typeof quote.requestedCode === 'string' ? normalizeMarketWatchCode(quote.requestedCode) : ''
    const symbol = typeof quote.symbol === 'string' ? quote.symbol : ''
    if (!expectedCodes.includes(requestedCode) || symbol !== marketWatchSymbol(requestedCode)) return null
    if (!isNullableFiniteNumber(quote.price) || !isNullableFiniteNumber(quote.changePct) || !isNullableFiniteNumber(quote.previousClose) || !isNullableFiniteNumber(quote.volume)) return null
    if (quote.asOf !== null && typeof quote.asOf !== 'string') return null
    return {
      requestedCode,
      symbol,
      price: quote.price as number | null,
      changePct: quote.changePct as number | null,
      previousClose: quote.previousClose as number | null,
      volume: quote.volume as number | null,
      asOf: quote.asOf as string | null,
    }
  })
  if (quotes.some((quote): quote is null => quote === null)) return null
  const normalizedQuotes = quotes as MarketWatchQuote[]
  if (!sameCodes(expectedCodes, normalizedQuotes.map((quote) => quote.requestedCode))) return null
  return {
    state: 'ready',
    source: 'tickflow',
    requestedCodes: expectedCodes,
    quotes: normalizedQuotes,
    fetchedAt: raw.fetchedAt,
    fromCache: true,
  }
}

function normalizeCodes(codes: string[]): string[] {
  return Array.from(new Set(codes.map(normalizeMarketWatchCode).filter(Boolean)))
}

function sameCodes(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false
  const a = [...left].sort()
  const b = [...right].sort()
  return a.every((code, index) => code === b[index])
}

function isNullableFiniteNumber(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isFinite(value))
}

export function formatMarketWatchContext(snapshot: MarketWatchSnapshot, selectedCodes?: string[]): string {
  if (snapshot.state === 'empty') return ''
  if (snapshot.state === 'unavailable') {
    return [
      '## 观察篮临时行情',
      `本轮没有成功取得观察篮最新报价。原因：${snapshot.message || '数据源暂时不可用'}。`,
      '不要根据缺失的报价猜测价格或涨跌幅；如果需要实时数据，应明确说明当前数据不可用。',
    ].join('\n')
  }

  const selected = selectedCodes ? new Set(selectedCodes.map(normalizeMarketWatchCode)) : null
  const rows = snapshot.quotes
    .filter((quote) => !selected || selected.has(quote.requestedCode))
    .map((quote) => {
    const price = quote.price == null ? '暂无' : quote.price.toFixed(4).replace(/\.?0+$/, '')
    const change = quote.changePct == null ? '暂无' : `${quote.changePct >= 0 ? '+' : ''}${quote.changePct.toFixed(2)}%`
    const volume = quote.volume == null ? '暂无' : String(Math.round(quote.volume))
      return `${quote.requestedCode} | 最新价=${price} | 涨跌幅=${change} | 成交量=${volume} | 时间=${quote.asOf || snapshot.fetchedAt}`
    })
  if (rows.length === 0) return ''

  return [
    `## 观察篮临时行情（${snapshot.fromCache ? '来自浏览器缓存，仍在有效期内' : '刚从 TickFlow 更新'}，不写入数据库）`,
    `行情来源：${snapshot.fromCache ? '浏览器本地缓存' : 'TickFlow'}；快照时间：${snapshot.fetchedAt}。这是最新可用行情，不一定是实时成交价。`,
    '以下是与用户当前问题相关的观察篮报价。只能引用返回的数字，不要把缺失值补成估算值。',
    '代码 | 最新价 | 涨跌幅 | 成交量 | 时间',
    ...rows,
  ].join('\n')
}
