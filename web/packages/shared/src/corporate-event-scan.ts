import { fetchEastMoneyNews, type RawNewsItem } from './news-chart-events'

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
  const only = matches.length === 1 ? matches[0] : undefined
  if (!only || /(?:编号|金额|订单|日期)[：: ]*$/.test(text.slice(0, only.index))) return { code: '', name: '' }
  return { code: only.groups?.code || '', name: '' }
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
      const oldest = stamps.at(0)
      const newest = stamps.at(-1)
      return { items, source: { source, ok: true, item_count: items.length, error: '', oldest_at: oldest === undefined ? '' : corporateShanghaiTime(oldest), newest_at: newest === undefined ? '' : corporateShanghaiTime(newest) } }
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
