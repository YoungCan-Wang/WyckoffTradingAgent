replace('web/packages/shared/src/news-chart-events.ts', '  pages = MAX_PAGES,\n', '  pages = MAX_PAGES,\n  strict = false,\n')
replace('web/packages/shared/src/news-chart-events.ts', '    if (!Array.isArray(batch) || batch.length === 0) break', "    if (!Array.isArray(batch)) {\n      if (strict) throw new Error('Invalid East Money news payload')\n      break\n    }\n    if (strict && batch.some((item) => !isRecord(item))) throw new Error('Invalid East Money article')\n    if (batch.length === 0) break")
replace('web/packages/shared/src/news-chart-events.ts', '  const response = await fetcher(`${EASTMONEY_NEWS_URL}?${params}`, {\n    headers:', '  const response = await fetcher(`${EASTMONEY_NEWS_URL}?${params}`, {\n    signal: AbortSignal.timeout(12_000),\n    headers:')
replace('web/packages/shared/src/news-chart-events.ts', "String(item.date || '').slice(0, 19)", "String(item.date || '')")
write('web/packages/shared/src/corporate-event-scan.ts', r'''import { fetchEastMoneyNews, type RawNewsItem } from './news-chart-events'

export const XINGSHUAIER_TELEGRAPH = '星帅尔002860：筹划购买PCB刀具设备公司湘鹰新材料等100%股权，股票停牌'
export const XINHUA_MEDIA_TELEGRAPH = '新华传媒600825：筹划重大资产重组，股票停牌'
export const SEARCH_KEYWORDS = ['重大资产重组', '股票停牌', '筹划购买', '借壳'] as const
export const CORPORATE_SOURCE_NOTE = '来源为东财关键词检索与财联社最新电报，不是全量公告核对；未命中不等于无事件。'
const MATERIAL_PHRASES = ['重大资产重组', '资产重组', '并购重组', '借壳', '筹划购买'] as const
const EQUITY_PHRASES = ['100%股权', '全部股权', '控股权'] as const
const HALT_CONTEXT = ['重组', '并购', '借壳', '筹划', '收购', '购买', '股权'] as const
const TELEGRAPH = /^(?<name>[\u4e00-\u9fffA-Za-z*＊]{2,12})\s*(?<code>[034689]\d{5})(?!\d)[：:]/
const PAREN_CODE = /(?<name>[\u4e00-\u9fffA-Za-z*＊]{2,12})[（(](?<code>[034689]\d{5})(?!\d)[)）]/
const LOOSE_CODE = /(?<![\dA-Za-z])(?<code>[034689]\d{5})(?![\dA-Za-z])/g
const DENIED = /(?:未|没有|不存在|不)(?:正在)?(?:筹划|涉及|进行)[^，；。]{0,16}(?:重组|并购|借壳|购买)|否认[^，；。]{0,20}(?:重组|借壳)|(?:重组|借壳)[^，；。]{0,12}(?:不实|无依据)/
const TERMINATED = /(?:终止|取消|不再推进)[^，；。]{0,16}(?:重组|并购|购买|收购|交易)/
const NOT_HALTED = /(?:不|无需|不会|未|不涉及)(?:申请)?停牌/
const NOT_RESUMED = /(?:未|不|暂不|尚未)复牌/

export interface CorporateEventHit {
  code: string
  name: string
  title: string
  reason: string
  source: string
  published_at: string
  event_status: string
  halt_status: string
  url: string
}

export interface CorporateSourceResult {
  source: string
  ok: boolean
  item_count: number
  error: string
  oldest_at: string
  newest_at: string
}

export interface CorporateEventCollection {
  items: RawNewsItem[]
  sources: CorporateSourceResult[]
  status: 'ok' | 'partial' | 'unavailable'
}

export function isMaterialRestructureOrHalt(title: string, content = ''): boolean {
  const text = `${title} ${content}`
  return [...MATERIAL_PHRASES, ...EQUITY_PHRASES].some((word) => text.includes(word))
    || (['停牌', '复牌'].some((word) => text.includes(word)) && HALT_CONTEXT.some((word) => text.includes(word)))
}

export function corporateEventStatus(title: string, content = ''): { event_status: string; halt_status: string } {
  const text = isMaterialRestructureOrHalt(title) ? title : `${title} ${content}`
  const resumed = text.includes('复牌') && !NOT_RESUMED.test(text)
  const halt_status = NOT_HALTED.test(text) ? 'not_halted' : resumed ? 'resumed' : text.includes('停牌') ? 'announced' : 'unknown'
  if (DENIED.test(text)) return { event_status: 'denied', halt_status }
  if (TERMINATED.test(text)) return { event_status: 'terminated', halt_status }
  if (resumed) return { event_status: 'resumed', halt_status }
  const announced = ['筹划', '预案', '拟购买', '拟收购', '审议通过', '股票停牌'].some((word) => text.includes(word)) && halt_status !== 'not_halted'
  return { event_status: announced ? 'announced' : 'unknown', halt_status }
}

export function classifyCorporateEventReason(title: string, content = ''): string {
  const { event_status, halt_status } = corporateEventStatus(title, content)
  if (event_status !== 'announced') return event_status
  const text = `${title} ${content}`
  const restructure = [...MATERIAL_PHRASES, ...EQUITY_PHRASES, '重组', '并购', '借壳'].some((word) => text.includes(word))
  if (halt_status === 'announced') return restructure ? 'halt_and_restructure' : 'halt_with_deal'
  return restructure ? 'restructure' : 'material_deal'
}

export function normalizeCorporateStockCode(raw: unknown): string {
  return /^(?:sh|sz|bj)?([034689]\d{5})(?:\.(?:sh|sz|bj))?$/i.exec(String(raw || '').trim())?.[1] || ''
}

export function parseTelegraphSymbol(title: string): { code: string; name: string } {
  const text = title.trim()
  const match = TELEGRAPH.exec(text) || PAREN_CODE.exec(text)
  if (match) return { code: match.groups?.code || '', name: match.groups?.name || '' }
  const matches = [...text.matchAll(LOOSE_CODE)]
  if (matches.length !== 1 || /(?:编号|金额|订单|日期)[：: ]*$/.test(text.slice(0, matches[0].index))) return { code: '', name: '' }
  return { code: matches[0].groups?.code || '', name: '' }
}

export function corporateEventTime(raw: string): number | null {
  const text = raw.trim()
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?)?$/.exec(text)
  if (!match) return null
  const [, year, month, day, hour, minute, second, offset] = match
  const calendarDay = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)))
  if (calendarDay.toISOString().slice(0, 10) !== text.slice(0, 10) || Number(hour || 0) > 23 || Number(minute || 0) > 59 || Number(second || 0) > 59) return null
  const iso = hour === undefined ? `${text}T23:59:59.999+08:00` : text.replace(' ', 'T') + (offset ? '' : '+08:00')
  const stamp = Date.parse(iso)
  return Number.isFinite(stamp) ? stamp : null
}

export function corporateShanghaiTime(stamp: number): string {
  return new Date(stamp + 8 * 60 * 60 * 1000).toISOString().replace('Z', '+08:00')
}

export function filterRecentCorporateHits(hits: CorporateEventHit[], asOf: string, lookbackDays = 2): CorporateEventHit[] {
  const end = corporateEventTime(asOf)
  if (end === null) throw new Error('Invalid observation cutoff')
  const start = end - Math.max(lookbackDays, 0) * 86_400_000
  return hits.filter((hit) => {
    const stamp = corporateEventTime(hit.published_at)
    return stamp !== null && stamp >= start && stamp <= end
  })
}

export function scanCorporateEvents(items: RawNewsItem[]): CorporateEventHit[] {
  const seen = new Set<string>()
  const hits: CorporateEventHit[] = []
  for (const item of items) {
    const hit = hitFromItem(item)
    if (!hit) continue
    const key = `${hit.code}:${hit.title.toLowerCase().replace(/[^\p{L}\p{N}_]/gu, '')}:${hit.published_at}`
    if (!seen.has(key)) { seen.add(key); hits.push(hit) }
  }
  return hits.sort((a, b) => (corporateEventTime(b.published_at) ?? -Infinity) - (corporateEventTime(a.published_at) ?? -Infinity) || b.code.localeCompare(a.code))
}

export function renderCorporateEventReport(
  hits: CorporateEventHit[], asOf: string, sourceStatus = 'ok', failedSources: string[] = [], undatedCount = 0,
): string {
  const lines = [`公司大事 / 停牌扫描 ${asOf}（Asia/Shanghai）`, '已公告与媒体电报观察，不是实盘，也不是漏斗买许可。', CORPORATE_SOURCE_NOTE]
  if (sourceStatus !== 'ok') {
    lines.push(sourceStatus === 'unavailable' ? '全部消息源不可用，无法判断是否有事件。' : '部分消息源不可用，以下仅为已获取的观察结果。', `不可用来源：${failedSources.join('、')}`)
  }
  if (undatedCount) lines.push(`另有 ${undatedCount} 条发布时间缺失或无效，未计入近期结果，需核实时间。`)
  if (!hits.length && sourceStatus !== 'unavailable') lines.push('本次检索窗口未命中；不代表没有停牌或重组。')
  for (const hit of hits) {
    lines.push(`- ${`${hit.code} ${hit.name}`.trim() || '未解析代码'} | ${hit.reason} / ${hit.halt_status} | ${hit.source} ${hit.published_at} | ${hit.title}`)
    if (/^https?:\/\//.test(hit.url)) lines.push(`  原文：${hit.url}`)
  }
  return lines.join('\n')
}

export async function collectCorporateEventItems(fetcher: typeof fetch = fetch): Promise<CorporateEventCollection> {
  const jobs: Array<[string, () => Promise<RawNewsItem[]>]> = SEARCH_KEYWORDS.map((keyword) => [
    `eastmoney:${keyword}`, () => fetchEastMoneyNews(keyword, fetcher, 2, true),
  ])
  jobs.push(['cls', () => fetchCorporateClsTelegraphs(fetcher)])
  const batches = await Promise.all(jobs.map(async ([source, fetchItems]) => {
    try {
      const items = await fetchItems()
      const stamps = items.map((item) => corporateEventTime(item.published_at || item.date || '')).filter((stamp): stamp is number => stamp !== null).sort((a, b) => a - b)
      return { items, source: { source, ok: true, item_count: items.length, error: '', oldest_at: stamps.length ? corporateShanghaiTime(stamps[0]) : '', newest_at: stamps.length ? corporateShanghaiTime(stamps[stamps.length - 1]) : '' } }
    } catch (error) {
      return { items: [], source: { source, ok: false, item_count: 0, error: error instanceof Error ? error.name : 'Error', oldest_at: '', newest_at: '' } }
    }
  }))
  const sources = batches.map((batch) => batch.source)
  const successes = sources.filter((source) => source.ok).length
  return { items: batches.flatMap((batch) => batch.items), sources, status: !successes ? 'unavailable' : successes === sources.length ? 'ok' : 'partial' }
}

export async function fetchCorporateClsTelegraphs(fetcher: typeof fetch): Promise<RawNewsItem[]> {
  const params = new URLSearchParams({ app: 'CailianpressWeb', os: 'web', sv: '8.4.6' })
  const response = await fetcher(`https://www.cls.cn/nodeapi/updateTelegraphList?${params}`, {
    headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://www.cls.cn/telegraph' }, signal: AbortSignal.timeout(8_000),
  })
  if (!response.ok) throw new Error('CLS HTTP failure')
  const payload: unknown = await response.json()
  const data = isRecord(payload) ? payload.data : null
  if (!isRecord(data) || !Array.isArray(data.roll_data)) throw new Error('Invalid CLS telegraph payload')
  if (data.roll_data.some((item: unknown) => !isRecord(item))) throw new Error('Invalid CLS telegraph row')
  return data.roll_data.filter(isRecord).map(normalizeCls)
}

function normalizeCls(item: Record<string, unknown>): RawNewsItem {
  const stocks = item.stock_list || item.stocks
  const first = Array.isArray(stocks) && stocks.length === 1 && isRecord(stocks[0]) ? stocks[0] : {}
  const content = String(item.content || item.descr || '').trim()
  const rawTime = item.ctime || item.time
  const stamp = Number(rawTime)
  const milliseconds = stamp > 1e12 ? stamp : stamp * 1000
  const timestamp = rawTime && Number.isFinite(milliseconds) && Math.abs(milliseconds) <= 8.64e15
    ? corporateShanghaiTime(milliseconds) : String(rawTime || '')
  return { title: String(item.title || item.brief || '').trim() || content.slice(0, 80), content,
    code: normalizeCorporateStockCode(first.StockID || first.code), name: String(first.name || first.secu_name || ''),
    source: '财联社', published_at: timestamp, url: String(item.shareurl || item.url || '') }
}

function hitFromItem(item: RawNewsItem): CorporateEventHit | null {
  const title = item.title.trim()
  const content = item.content || ''
  if (!title || !isMaterialRestructureOrHalt(title, content)) return null
  const parsed = parseTelegraphSymbol(title)
  const symbol = parsed.code ? parsed : parseTelegraphSymbol(content)
  return { code: normalizeCorporateStockCode(item.code) || symbol.code, name: (item.name || '').trim() || symbol.name,
    title, reason: classifyCorporateEventReason(title, content), ...corporateEventStatus(title, content),
    source: item.source || 'media', published_at: item.published_at || item.date || '', url: item.url || '' }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
''')
replace('web/packages/shared/src/chat-tools.ts', '  collectCorporateEventItems,\n', '  collectCorporateEventItems,\n  corporateEventTime,\n  corporateShanghaiTime,\n  filterRecentCorporateHits,\n')
p = r/'web/packages/shared/src/chat-tools.ts'
s = p.read_text()
start = s.index('export async function execScanCorporateEvents')
end = s.index('\nfunction formatNewsHeadlineLine', start)
s = s[:start] + '''export async function execScanCorporateEvents(deps: ToolDeps, limit = 20, asOf = corporateShanghaiTime(Date.now())): Promise<string> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) return 'limit 必须是 1–50 的整数。'
  const collection = await collectCorporateEventItems(deps.fetch)
  const classified = scanCorporateEvents(collection.items)
  const undatedCount = classified.filter((hit) => corporateEventTime(hit.published_at) === null).length
  const hits = filterRecentCorporateHits(classified, asOf).slice(0, limit)
  return renderCorporateEventReport(hits, asOf, collection.status, collection.sources.filter((source) => !source.ok).map((source) => source.source), undatedCount)
}
''' + s[end:]
p.write_text(s)
replace('web/apps/api/src/routes/chat.ts', 'inputSchema: z.object({ limit: z.number().nullable() }), execute: ({ limit }) => execScanCorporateEvents', 'inputSchema: z.object({ limit: z.number().int().min(1).max(50).nullable() }), execute: ({ limit }) => execScanCorporateEvents')
replace('web/apps/api/src/routes/chat.ts', '这是全市场观察，不是漏斗买许可，也不是实盘。', '这是有限来源的最近48小时观察，不是全量公告核对、漏斗买许可或实盘。必须保留消息源异常和时间未知提示，不得把否认、终止或复牌解释为新停牌。')
write('web/apps/web/src/lib/__tests__/corporate-event-scan.test.ts', r'''import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  XINGSHUAIER_TELEGRAPH, XINHUA_MEDIA_TELEGRAPH, classifyHeadline, execScanCorporateEvents,
  isMaterialRestructureOrHalt, parseTelegraphSymbol, scanCorporateEvents, collectCorporateEventItems,
  filterRecentCorporateHits, corporateEventTime, corporateShanghaiTime, type ToolDeps,
} from '@wyckoff/shared'

const AS_OF = '2026-09-21T19:40:00+08:00'
const cases = JSON.parse(readFileSync(new URL('../../../../../../tests/fixtures/corporate_event_cases.json', import.meta.url), 'utf8')) as Array<{
  title: string; content?: string; code: string; event_status: string; halt_status: string; reason: string
}>

function depsWith(fetchImpl: typeof globalThis.fetch): ToolDeps {
  return { supabase: null as never, fetch: fetchImpl, generateText: null as never }
}

function eastMoneyResponse(articles: Array<Record<string, string>>): Response {
  return new Response(`jQuery3510(${JSON.stringify({ result: { cmsArticleWebOld: articles } })})`)
}

function emptyCls(): Response {
  return Response.json({ data: { roll_data: [] } })
}

describe('corporate event scan', () => {
  it.each(cases)('shares Python classification semantics: $title', (example) => {
    const [hit] = scanCorporateEvents([{ title: example.title, content: example.content }])
    expect(hit).toMatchObject({ code: example.code, event_status: example.event_status, halt_status: example.halt_status, reason: example.reason })
  })

  it('keeps the positive examples and ignores ordinary price noise', () => {
    expect(isMaterialRestructureOrHalt(XINGSHUAIER_TELEGRAPH)).toBe(true)
    expect(parseTelegraphSymbol(XINGSHUAIER_TELEGRAPH)).toEqual({ code: '002860', name: '星帅尔' })
    expect(scanCorporateEvents([{ title: XINHUA_MEDIA_TELEGRAPH }, { title: '某股20cm涨停，龙虎榜净买入' }]).map((hit) => hit.code)).toEqual(['600825'])
    expect(['risk', 'deal']).toContain(classifyHeadline(XINGSHUAIER_TELEGRAPH)?.kind)
  })

  it('queries two East Money pages and CLS, preserving source health', async () => {
    const keywords: string[] = []
    const text = await execScanCorporateEvents(depsWith(async (url, init) => {
      if (String(url).includes('cls.cn')) return emptyCls()
      const params = JSON.parse(new URL(String(url)).searchParams.get('param') || '{}')
      keywords.push(params.keyword)
      expect(new Set(keywords).size).toBeGreaterThan(0)
      expect(new Headers(init?.headers).get('Referer')).toMatch(/keyword=%/)
      expect(init?.signal).toBeDefined()
      return eastMoneyResponse([{ date: '2026-09-21 19:20:00', title: XINGSHUAIER_TELEGRAPH, mediaName: '财联社' }])
    }), 20, AS_OF)
    expect(new Set(keywords).size).toBe(4)
    expect(text).toContain('002860')
    expect(text).toContain('不是实盘')
    expect(text).toContain('Asia/Shanghai')
    expect(text).not.toContain('消息源不可用')
  })

  it('can discover a CLS-only event even when East Money has no results', async () => {
    const fetcher: typeof fetch = async (url) => String(url).includes('cls.cn')
      ? Response.json({ data: { roll_data: [{ content: XINGSHUAIER_TELEGRAPH, ctime: Date.parse('2026-09-21T19:20:00+08:00') / 1000,
        stock_list: [{ StockID: 'sz002860', name: '星帅尔' }], shareurl: 'https://www.cls.cn/detail/1' }] } })
      : eastMoneyResponse([])
    const collection = await collectCorporateEventItems(fetcher)
    expect(collection.status).toBe('ok')
    expect(collection.sources).toHaveLength(5)
    const text = await execScanCorporateEvents(depsWith(fetcher), 20, AS_OF)
    expect(text).toContain('002860')
    expect(text).toContain('https://www.cls.cn/detail/1')
    expect(text).toContain('19:20:00')
  })

  it.each(['throw', '503', 'malformed'])('does not turn all source failures into an empty scan: %s', async (failure) => {
    const fetcher: typeof fetch = async () => {
      if (failure === 'throw') throw new Error('private credential in upstream URL')
      return failure === '503' ? new Response('upstream down', { status: 503 }) : new Response('{}')
    }
    const collection = await collectCorporateEventItems(fetcher)
    expect(collection.status).toBe('unavailable')
    expect(collection.sources.every((source) => !source.ok)).toBe(true)
    const text = await execScanCorporateEvents(depsWith(fetcher), 20, AS_OF)
    expect(text).toContain('全部消息源不可用')
    expect(text).not.toContain('本次检索窗口未命中')
    expect(text).not.toContain('private credential')
  })

  it('keeps partial results but identifies the failed source', async () => {
    const text = await execScanCorporateEvents(depsWith(async (url) => {
      if (String(url).includes('cls.cn')) return new Response('', { status: 503 })
      return eastMoneyResponse([{ date: '2026-09-21 19:20:00', title: XINGSHUAIER_TELEGRAPH }])
    }), 20, AS_OF)
    expect(text).toContain('部分消息源不可用')
    expect(text).toContain('不可用来源：cls')
    expect(text).toContain('002860')
  })

  it.each(['2026-09-21 08:15', '2026-09-21T00:15:00Z', '2026-09-20T20:15:00-04:00'])('filters exact cutoffs, offsets and unknown dates: %s', (asOf) => {
    const dates = ['2026-09-21 08:15:00', '2026-09-21 19:20:00', '2026-09-19 08:14:59', '2026-09-19 08:15:00', '', 'not-a-time', '2026-09-21']
    const hits = scanCorporateEvents(dates.map((date) => ({ title: XINGSHUAIER_TELEGRAPH, published_at: date })))
    expect(filterRecentCorporateHits(hits, asOf).map((hit) => hit.published_at).sort()).toEqual([dates[0], dates[3]].sort())
    expect(corporateShanghaiTime(corporateEventTime(asOf)!)).toBe('2026-09-21T08:15:00.000+08:00')
  })

  it('does not surface stale or undated news as current', async () => {
    const text = await execScanCorporateEvents(depsWith(async (url) => String(url).includes('cls.cn') ? emptyCls() : eastMoneyResponse([
      { title: '过期公司600825：筹划重大资产重组，股票停牌', date: '2026-09-01 19:20:00' },
      { title: XINGSHUAIER_TELEGRAPH, date: '' },
    ])), 20, AS_OF)
    expect(text).not.toContain('过期公司')
    expect(text).not.toContain('002860')
    expect(text).toContain('未计入近期结果')
  })

  it('rejects invalid timestamps instead of Date rolling them forward', () => {
    expect(corporateEventTime('2026-02-30 08:15')).toBeNull()
    expect(corporateEventTime('2026-09-21 25:00')).toBeNull()
    expect(() => filterRecentCorporateHits([], 'bad')).toThrow()
  })

  it('rejects invalid tool limits before making requests', async () => {
    for (const limit of [0, -1, 51, 1.5, NaN]) {
      const result = await execScanCorporateEvents(depsWith(async () => { throw new Error('must not fetch') }), limit, AS_OF)
      expect(result).toContain('limit 必须')
    }
  })
})
''')
