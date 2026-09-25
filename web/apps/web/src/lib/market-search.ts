export type SearchMarket = 'cn' | 'hk' | 'us'

export interface StockSearchResult {
  analysisCode: string
  symbol: string
  code: string
  name: string
  market: SearchMarket
  assetType: string
  aliases: string[]
}

interface IndexedStockSearchResult extends StockSearchResult {
  tokens: string[]
}

interface RawMarketRow {
  symbol?: unknown
  code?: unknown
  name?: unknown
  market?: unknown
  asset_type?: unknown
  aliases?: unknown
}

const MARKET_LABELS: Record<SearchMarket, string> = {
  cn: 'A股/ETF',
  hk: '港股',
  us: '美股',
}

const SEARCH_FILES = {
  cn: '/market-data/stock_list_cache.json',
  etf: '/market-data/etf_cn_meta.json',
  us: '/market-data/us_meta.json',
  hk: '/market-data/hk_meta.json',
  aliases: '/market-data/aliases.json',
} as const

interface SearchIndex {
  items: IndexedStockSearchResult[]
  prefixMap: Map<string, IndexedStockSearchResult[]>
}

let searchIndexPromise: Promise<SearchIndex> | null = null

export function marketLabel(market: SearchMarket): string {
  return MARKET_LABELS[market]
}

export function resetStockSearchCacheForTest(): void {
  searchIndexPromise = null
}

export async function searchStocks(query: string, limit = 8): Promise<StockSearchResult[]> {
  const q = normalizeSearchText(query)
  if (!q) return []
  const { items, prefixMap } = await loadSearchIndex()
  const candidates = getCandidates(q, items, prefixMap)
  return candidates
    .map((item) => ({ item, score: scoreMatch(item, q) }))
    .filter((row) => Number.isFinite(row.score))
    .sort((a, b) => a.score - b.score || marketRank(a.item.market) - marketRank(b.item.market))
    .slice(0, limit)
    .map(({ item }) => stripIndexFields(item))
}

export async function resolveStockQuery(query: string): Promise<StockSearchResult | null> {
  const [first] = await searchStocks(query, 1)
  return first ?? null
}

async function loadSearchIndex(): Promise<SearchIndex> {
  searchIndexPromise ??= buildSearchIndex().then(buildPrefixIndex).catch((err) => {
    searchIndexPromise = null
    throw err
  })
  return searchIndexPromise
}

function buildPrefixIndex(items: IndexedStockSearchResult[]): SearchIndex {
  const prefixMap = new Map<string, IndexedStockSearchResult[]>()
  for (const item of items) {
    const seen = new Set<string>()
    for (const token of item.tokens) {
      const k1 = token.slice(0, 1)
      const k2 = token.slice(0, 2)
      if (k1 && !seen.has(k1)) { seen.add(k1); pushTo(prefixMap, k1, item) }
      if (k2 && k2 !== k1 && !seen.has(k2)) { seen.add(k2); pushTo(prefixMap, k2, item) }
    }
  }
  return { items, prefixMap }
}

function pushTo(map: Map<string, IndexedStockSearchResult[]>, key: string, item: IndexedStockSearchResult) {
  const arr = map.get(key)
  if (arr) arr.push(item)
  else map.set(key, [item])
}

function getCandidates(q: string, items: IndexedStockSearchResult[], prefixMap: Map<string, IndexedStockSearchResult[]>): IndexedStockSearchResult[] {
  const k2 = q.slice(0, 2)
  const k1 = q.slice(0, 1)
  return prefixMap.get(k2) || prefixMap.get(k1) || items
}

async function buildSearchIndex(): Promise<IndexedStockSearchResult[]> {
  const [cnRows, etfRows, usRows, hkRows, aliasRows] = await Promise.all([
    fetchMarketRows(SEARCH_FILES.cn),
    fetchMarketRows(SEARCH_FILES.etf),
    fetchMarketRows(SEARCH_FILES.us),
    fetchMarketRows(SEARCH_FILES.hk),
    fetchMarketRows(SEARCH_FILES.aliases),
  ])
  const byKey = new Map<string, StockSearchResult>()
  for (const row of cnRows) addOrMerge(byKey, normalizeRow(row, 'cn', 'stock'))
  for (const row of etfRows) addOrMerge(byKey, normalizeRow(row, 'cn', 'etf'))
  for (const row of usRows) addOrMerge(byKey, normalizeRow(row, 'us', 'stock'))
  for (const row of hkRows) addOrMerge(byKey, normalizeRow(row, 'hk', 'stock'))
  for (const row of aliasRows) addOrMerge(byKey, normalizeRow(row, normalizeMarket(row.market), String(row.asset_type || 'stock')))

  return Array.from(byKey.values(), (item) => ({
    ...item,
    tokens: buildTokens(item),
  }))
}

async function fetchMarketRows(url: string): Promise<RawMarketRow[]> {
  try {
    // force-cache keeps the first download even after a hard reload, so a newer
    // universe file never reaches the search box.
    const response = await fetch(url, { cache: 'no-cache' })
    if (!response.ok) return []
    const rows: unknown = await response.json()
    return Array.isArray(rows) ? rows : []
  } catch {
    return []
  }
}

function normalizeRow(row: RawMarketRow, fallbackMarket: SearchMarket, fallbackAssetType: string): StockSearchResult | null {
  const market = normalizeMarket(row.market, fallbackMarket)
  const code = String(row.code || '').trim().toUpperCase()
  const rawSymbol = String(row.symbol || '').trim().toUpperCase()
  const symbol = rawSymbol || code
  const cleanCode = code || symbol.split('.')[0] || symbol
  const name = String(row.name || '').trim()
  if (!symbol && !cleanCode) return null
  return {
    analysisCode: market === 'cn' ? cleanCode : symbol,
    symbol: market === 'cn' ? symbol || cleanCode : symbol,
    code: cleanCode,
    name,
    market,
    assetType: String(row.asset_type || fallbackAssetType || 'stock').trim() || 'stock',
    aliases: normalizeAliases(row.aliases),
  }
}

function normalizeMarket(value: unknown, fallback: SearchMarket = 'cn'): SearchMarket {
  const raw = String(value || '').trim().toLowerCase()
  if (raw === 'hk') return 'hk'
  if (raw === 'us') return 'us'
  return fallback
}

function normalizeAliases(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return Array.from(new Set(value.map((item) => String(item || '').trim()).filter(Boolean)))
}

function addOrMerge(byKey: Map<string, StockSearchResult>, maybeItem: StockSearchResult | null): void {
  if (!maybeItem) return
  const key = `${maybeItem.market}:${maybeItem.analysisCode}`
  const current = byKey.get(key)
  if (!current) {
    byKey.set(key, maybeItem)
    return
  }
  current.name = maybeItem.name || current.name
  current.symbol = maybeItem.symbol || current.symbol
  current.assetType = current.assetType === 'stock' ? maybeItem.assetType : current.assetType
  current.aliases = Array.from(new Set([...current.aliases, ...maybeItem.aliases]))
}

function buildTokens(item: StockSearchResult): string[] {
  return [item.analysisCode, item.symbol, item.code, item.name, ...item.aliases]
    .map(normalizeSearchText)
    .filter(Boolean)
}

function scoreMatch(item: IndexedStockSearchResult, query: string): number {
  let best = Number.POSITIVE_INFINITY
  for (const token of item.tokens) {
    if (token === query) best = Math.min(best, 0)
    else if (token.startsWith(query)) best = Math.min(best, 10 + token.length / 100)
    else if (token.includes(query)) best = Math.min(best, 30 + token.indexOf(query) / 100)
  }
  return best
}

function marketRank(market: SearchMarket): number {
  if (market === 'cn') return 0
  if (market === 'hk') return 1
  return 2
}

function normalizeSearchText(value: string): string {
  return value.normalize('NFKC').trim().toUpperCase().replace(/\s+/g, '')
}

function stripIndexFields(item: IndexedStockSearchResult): StockSearchResult {
  return {
    analysisCode: item.analysisCode,
    symbol: item.symbol,
    code: item.code,
    name: item.name || item.analysisCode,
    market: item.market,
    assetType: item.assetType,
    aliases: item.aliases,
  }
}
