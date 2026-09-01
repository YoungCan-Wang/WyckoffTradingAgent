export type NewsEventKind = 'regulatory' | 'risk' | 'earnings' | 'holder' | 'deal'
export type NewsSentiment = 'bullish' | 'bearish' | 'mixed' | 'unknown'

export interface RawNewsItem {
  title: string
  content?: string
  published_at?: string
  date?: string
  source?: string
  url?: string
}

export interface NewsChartEvent {
  date: string
  kind: NewsEventKind
  sentiment: NewsSentiment
  title: string
  summary: string
  source: string
  url: string
  score: number
}

const KIND_WEIGHTS: Record<NewsEventKind, number> = {
  regulatory: 5,
  risk: 4,
  earnings: 4,
  holder: 3,
  deal: 3,
}

const KIND_KEYWORDS: Record<NewsEventKind, readonly string[]> = {
  regulatory: ['立案', '调查', '处罚', '问询函', '证监会', '监管函'],
  risk: ['风险提示', '停牌', '退市', '债务违约', '暴雷'],
  earnings: ['业绩预增', '业绩预减', '业绩预亏', '扭亏', '年报', '中报', '一季报', '三季报'],
  holder: ['减持', '增持', '回购', '股权激励'],
  deal: ['中标', '签订合同', '战略投资', '入股', '定增', '收购'],
}

const BULLISH_KEYWORDS = ['预增', '扭亏', '增持', '回购', '中标', '入股', '超预期', '增长'] as const
const BEARISH_KEYWORDS = ['预减', '预亏', '减持', '立案', '调查', '处罚', '问询', '下滑', '违约'] as const
const NOISE_ONLY = ['涨停', '跌停', '连板', '龙虎榜', '20cm', '一字'] as const
const ROUNDUP_TITLES = ['集锦', '一览', '股今日获', '龙虎榜'] as const
const EASTMONEY_NEWS_URL = 'https://search-api-web.eastmoney.com/search/jsonp'
const PAGE_SIZE = 20
const MAX_PAGES = 4

export function selectNewsChartEvents(
  items: RawNewsItem[],
  start: string,
  end: string,
  sessionDates: string[],
  limit = 8,
  symbol = '',
  name = '',
): NewsChartEvent[] {
  if (!parseDay(start) || !parseDay(end) || end < start) return []
  const sessions = sessionDates.filter((day) => Boolean(parseDay(day)))
  const seen = new Set<string>()
  const scored: NewsChartEvent[] = []
  for (const item of items) {
    const event = eventFromItem(item, start, end, sessions, seen, symbol, name)
    if (event) scored.push(event)
  }
  scored.sort((a, b) => b.score - a.score || a.date.localeCompare(b.date) || a.title.localeCompare(b.title))
  return onePerDay(scored).slice(0, Math.max(limit, 0))
}

export function classifyHeadline(title: string, content = ''): { kind: NewsEventKind; sentiment: NewsSentiment; score: number } | null {
  const text = `${title} ${content}`
  if (isNoiseHeadline(title)) return null
  const kind = (Object.keys(KIND_KEYWORDS) as NewsEventKind[]).find((name) => KIND_KEYWORDS[name].some((word) => text.includes(word)))
  if (!kind) return null
  const extra = [...BULLISH_KEYWORDS, ...BEARISH_KEYWORDS].filter((word) => text.includes(word)).length
  return { kind, sentiment: headlineSentiment(text), score: KIND_WEIGHTS[kind] + extra }
}

/** 涨停板、龙虎榜、盘后集锦这类没有事件内核的标题。 */
export function isNoiseHeadline(title: string): boolean {
  return [...ROUNDUP_TITLES, ...NOISE_ONLY].some((word) => title.includes(word))
}

export function headlineSentiment(text: string): NewsSentiment {
  const bullish = BULLISH_KEYWORDS.some((word) => text.includes(word))
  const bearish = BEARISH_KEYWORDS.some((word) => text.includes(word))
  return bullish && bearish ? 'mixed' : bullish ? 'bullish' : bearish ? 'bearish' : 'unknown'
}

export function snapToSession(rawDate: string, sessionDates: string[]): string {
  const day = parseDay(rawDate)
  if (!day) return ''
  return sessionDates.find((session) => session >= day) || sessionDates.at(-1) || ''
}

export interface StockNewsHeadline {
  date: string
  title: string
  summary: string
  source: string
  url: string
  /** 命中已知事件类型时给出;命中不了仍保留条目 —— 核证消息不该只看这五类。 */
  kind: NewsEventKind | null
  sentiment: NewsSentiment
}

/**
 * 给模型核证用的消息流,与作图用的 selectNewsChartEvents 不是一回事。
 *
 * 作图那条要求「一天一个、必须归到已知事件类型、必须贴到交易日」,因为图上一天只
 * 挂得下一个标记。核证不受这些约束:同一天可以有两条,归不了类的公告照样是证据。
 * 所以这里只做三件事 —— 去噪、去重、按时间倒序,保留原始日期不贴任何交易日。
 */
export function selectStockNewsHeadlines(items: RawNewsItem[], limit = 12, symbol = '', name = ''): StockNewsHeadline[] {
  const seen = new Set<string>()
  const rows: StockNewsHeadline[] = []
  for (const item of items) {
    const title = item.title.trim()
    const published = parseDay(item.published_at || item.date || '')
    if (!title || !published) continue
    if (isNoiseHeadline(title)) continue
    if (!mentionsSymbol(title, item.content || '', symbol, name)) continue
    const key = titleKey(title)
    if (seen.has(key)) continue
    seen.add(key)
    rows.push({
      date: published,
      title,
      summary: (item.content || '').replace(/\s+/g, ' ').trim().slice(0, 100),
      source: item.source || 'eastmoney',
      url: item.url || '',
      kind: classifyHeadline(title, item.content || '')?.kind ?? null,
      sentiment: headlineSentiment(`${title} ${item.content || ''}`),
    })
  }
  rows.sort((a, b) => b.date.localeCompare(a.date) || a.title.localeCompare(b.title))
  return rows.slice(0, Math.max(limit, 0))
}

export async function fetchEastMoneyStockNews(code: string, fetcher: typeof fetch = fetch): Promise<RawNewsItem[]> {
  const rows: RawNewsItem[] = []
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const payload = await requestNewsPage(code, page, fetcher)
    const batch = payload?.result?.cmsArticleWebOld
    if (!Array.isArray(batch) || batch.length === 0) break
    rows.push(...batch.filter(isRecord).map(normalizeArticle))
    if (batch.length < PAGE_SIZE) break
  }
  return rows
}

export async function handleNewsEventsRequest(request: Request, fetcher: typeof fetch = fetch): Promise<Response> {
  const url = new URL(request.url)
  const code = (url.searchParams.get('code') || '').trim()
  const start = (url.searchParams.get('start') || '').trim()
  const end = (url.searchParams.get('end') || '').trim()
  const sessions = (url.searchParams.get('sessions') || '').split(',').map((day) => day.trim()).filter(Boolean)
  if (!/^\d{6}$/.test(code) || !parseDay(start) || !parseDay(end)) {
    return jsonResponse({ error: 'invalid_query', events: [] }, 400)
  }
  try {
    const name = (url.searchParams.get('name') || '').trim()
    const events = selectNewsChartEvents(await fetchEastMoneyStockNews(code, fetcher), start, end, sessions, 8, code, name)
    return jsonResponse({ events, note: 'chart_reading_overlay' })
  } catch {
    return jsonResponse({ error: 'upstream_failed', events: [] }, 502)
  }
}

async function requestNewsPage(code: string, page: number, fetcher: typeof fetch): Promise<EastMoneySearchPayload | null> {
  const params = new URLSearchParams({
    cb: 'jQuery3510',
    param: JSON.stringify(searchParam(code, page)),
    _: '1',
  })
  const response = await fetcher(`${EASTMONEY_NEWS_URL}?${params}`, {
    headers: {
      'User-Agent': 'Mozilla/5.0',
      Referer: `https://so.eastmoney.com/news/s?keyword=${code}`,
    },
  })
  if (!response.ok) return null
  return parseJsonp(await response.text())
}

function searchParam(code: string, page: number) {
  return {
    uid: '',
    keyword: code,
    type: ['cmsArticleWebOld'],
    client: 'web',
    clientType: 'web',
    clientVersion: 'curr',
    param: {
      cmsArticleWebOld: {
        searchScope: 'default',
        sort: 'default',
        pageIndex: page,
        pageSize: PAGE_SIZE,
        preTag: '',
        postTag: '',
      },
    },
  }
}

function eventFromItem(
  item: RawNewsItem,
  start: string,
  end: string,
  sessions: string[],
  seen: Set<string>,
  symbol: string,
  name: string,
): NewsChartEvent | null {
  const title = item.title.trim()
  const published = parseDay(item.published_at || item.date || '')
  if (!title || !published || published < start || published > end) return null
  if (!mentionsSymbol(title, item.content || '', symbol, name)) return null
  const classified = classifyHeadline(title, item.content || '')
  if (!classified) return null
  const session = snapToSession(published, sessions)
  if (!session) return null
  const fingerprint = `${session}:${titleKey(title)}`
  if (seen.has(fingerprint)) return null
  seen.add(fingerprint)
  return {
    date: session,
    kind: classified.kind,
    sentiment: classified.sentiment,
    title,
    summary: (item.content || title).replace(/\n/g, ' ').slice(0, 80),
    source: item.source || 'eastmoney',
    url: item.url || '',
    score: classified.score,
  }
}

function onePerDay(events: NewsChartEvent[]): NewsChartEvent[] {
  const used = new Set<string>()
  return events.filter((event) => {
    if (used.has(event.date)) return false
    used.add(event.date)
    return true
  })
}

function normalizeArticle(item: Record<string, unknown>): RawNewsItem {
  const code = String(item.code || '').trim()
  return {
    title: String(item.title || '').trim(),
    content: String(item.content || '').trim(),
    published_at: String(item.date || '').slice(0, 19),
    source: String(item.mediaName || 'eastmoney'),
    url: code ? `https://finance.eastmoney.com/a/${code}.html` : '',
  }
}

function parseJsonp(text: string): EastMoneySearchPayload | null {
  const start = text.indexOf('(')
  const end = text.lastIndexOf(')')
  if (start < 0 || end <= start) return null
  try {
    return JSON.parse(text.slice(start + 1, end)) as EastMoneySearchPayload
  } catch {
    return null
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  })
}

function mentionsSymbol(title: string, content: string, symbol: string, name: string): boolean {
  if (!symbol && !name) return true
  const text = `${title} ${content.slice(0, 80)}`
  return Boolean(symbol && text.includes(symbol)) || Boolean(name && text.includes(name))
}

function parseDay(raw: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(raw.slice(0, 10)) ? raw.slice(0, 10) : ''
}

function titleKey(title: string): string {
  return title.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '').slice(0, 24)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

interface EastMoneySearchPayload {
  result?: { cmsArticleWebOld?: unknown[] }
}
