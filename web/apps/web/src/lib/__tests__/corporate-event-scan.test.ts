import { readFileSync } from 'node:fs'
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
