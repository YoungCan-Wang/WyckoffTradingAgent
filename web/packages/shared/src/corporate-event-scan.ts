import { fetchEastMoneyNews, type RawNewsItem } from './news-chart-events'

export const XINGSHUAIER_TELEGRAPH = '星帅尔002860：筹划购买PCB刀具设备公司湘鹰新材料等100%股权，股票停牌'
export const XINHUA_MEDIA_TELEGRAPH = '新华传媒600825：筹划重大资产重组，股票停牌'
export const SEARCH_KEYWORDS = ['重大资产重组', '股票停牌', '筹划购买', '借壳'] as const

const MATERIAL_PHRASES = ['重大资产重组', '资产重组', '并购重组', '借壳', '筹划购买'] as const
const EQUITY_PHRASES = ['100%股权', '全部股权', '控股权'] as const
const HALT_WORDS = ['股票停牌', '停牌'] as const
const HALT_CONTEXT = ['重组', '并购', '借壳', '筹划', '收购', '购买', '股权'] as const
const TELEGRAPH = /^(?<name>[\u4e00-\u9fffA-Za-z0-9*＊]{2,12})?(?<code>\d{6})[：:](?<body>.+)/
const PAREN_CODE = /(?<name>[\u4e00-\u9fffA-Za-z0-9*＊]{2,12})[（(](?<code>\d{6})[)）]/
const LOOSE_CODE = /(?<name>[\u4e00-\u9fff]{2,8})?(?<code>\d{6})/

export interface CorporateEventHit {
  code: string
  name: string
  title: string
  reason: string
  source: string
  published_at: string
}

export function isMaterialRestructureOrHalt(title: string, content = ''): boolean {
  const text = `${title} ${content}`
  if (MATERIAL_PHRASES.some((phrase) => text.includes(phrase))) return true
  if (EQUITY_PHRASES.some((phrase) => text.includes(phrase))) return true
  return HALT_WORDS.some((word) => text.includes(word)) && HALT_CONTEXT.some((word) => text.includes(word))
}

export function classifyCorporateEventReason(title: string, content = ''): string {
  const text = `${title} ${content}`
  const halted = HALT_WORDS.some((word) => text.includes(word))
  const restructure = [...MATERIAL_PHRASES, ...EQUITY_PHRASES, '重组', '并购', '借壳'].some((phrase) => text.includes(phrase))
  if (halted && restructure) return 'halt_and_restructure'
  if (restructure) return 'restructure'
  if (halted) return 'halt_with_deal'
  return 'material_deal'
}

export function parseTelegraphSymbol(title: string): { code: string; name: string } {
  const match = TELEGRAPH.exec(title.trim()) || PAREN_CODE.exec(title) || LOOSE_CODE.exec(title)
  return { code: match?.groups?.code || '', name: match?.groups?.name || '' }
}

export function scanCorporateEvents(items: RawNewsItem[]): CorporateEventHit[] {
  const seen = new Set<string>()
  const hits: CorporateEventHit[] = []
  for (const item of items) {
    const hit = hitFromItem(item)
    if (!hit) continue
    const key = `${hit.code}:${titleKey(hit.title)}`
    if (seen.has(key)) continue
    seen.add(key)
    hits.push(hit)
  }
  return hits.sort((a, b) => b.published_at.localeCompare(a.published_at) || b.code.localeCompare(a.code) || b.title.localeCompare(a.title))
}

export function renderCorporateEventReport(hits: CorporateEventHit[], asOf: string): string {
  const header = [`公司大事 / 停牌扫描 ${asOf}`, '已公告与媒体电报观察，不是实盘，也不是漏斗买许可。', '']
  if (hits.length === 0) return [...header, '当日未扫到重大资产重组 / 停牌电报。'].join('\n')
  return [
    ...header,
    ...hits.map((hit) => {
      const label = `${hit.code} ${hit.name}`.trim() || hit.code || '未解析代码'
      return `- ${label} | ${hit.reason} | ${hit.source} ${hit.published_at} | ${hit.title}`
    }),
  ].join('\n')
}

export async function collectCorporateEventItems(fetcher: typeof fetch = fetch): Promise<RawNewsItem[]> {
  const batches = await Promise.all(SEARCH_KEYWORDS.map((keyword) => fetchEastMoneyNews(keyword, fetcher, 1).catch(() => [])))
  return batches.flat()
}

function hitFromItem(item: RawNewsItem): CorporateEventHit | null {
  const title = item.title.trim()
  const content = item.content || ''
  if (!title || !isMaterialRestructureOrHalt(title, content)) return null
  const parsed = parseTelegraphSymbol(title)
  const fromBody = parsed.code ? parsed : parseTelegraphSymbol(content)
  return {
    code: sixDigit(item.code) || fromBody.code,
    name: (item.name || '').trim() || fromBody.name,
    title,
    reason: classifyCorporateEventReason(title, content),
    source: item.source || 'media',
    published_at: item.published_at || item.date || '',
  }
}

function sixDigit(raw: unknown): string {
  const digits = String(raw || '').replace(/\D/g, '')
  return digits.length === 6 ? digits : ''
}

function titleKey(title: string): string {
  return title.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '').slice(0, 32)
}
