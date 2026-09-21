import { describe, expect, it } from 'vitest'
import {
  XINGSHUAIER_TELEGRAPH,
  XINHUA_MEDIA_TELEGRAPH,
  classifyHeadline,
  execScanCorporateEvents,
  isMaterialRestructureOrHalt,
  parseTelegraphSymbol,
  scanCorporateEvents,
  type ToolDeps,
} from '@wyckoff/shared'

function depsWith(fetchImpl: typeof globalThis.fetch): ToolDeps {
  return { supabase: null as never, fetch: fetchImpl, generateText: null as never }
}

function eastMoneyResponse(articles: Array<Record<string, string>>): Response {
  return new Response(`jQuery3510(${JSON.stringify({ result: { cmsArticleWebOld: articles } })})`)
}

describe('corporate event scan', () => {
  it('keeps the 星帅尔 002860 CLS telegraph as a required hit', () => {
    expect(isMaterialRestructureOrHalt(XINGSHUAIER_TELEGRAPH)).toBe(true)
    expect(parseTelegraphSymbol(XINGSHUAIER_TELEGRAPH)).toEqual({ code: '002860', name: '星帅尔' })
    const hits = scanCorporateEvents([{ title: XINGSHUAIER_TELEGRAPH, source: '财联社', published_at: '2026-09-21 19:20:00' }])
    expect(hits.map((hit) => hit.code)).toEqual(['002860'])
    expect(hits[0]?.reason).toBe('halt_and_restructure')
  })

  it('still hits 新华传媒 halt and ignores limit-up noise', () => {
    const hits = scanCorporateEvents([
      { title: XINHUA_MEDIA_TELEGRAPH, source: '财联社' },
      { title: '某股20cm涨停，龙虎榜净买入', published_at: '2026-09-21 15:10:00' },
    ])
    expect(hits.map((hit) => hit.code)).toEqual(['600825'])
  })

  it('keeps announced halt headlines on the chart overlay', () => {
    const classified = classifyHeadline(XINGSHUAIER_TELEGRAPH)
    expect(classified).not.toBeNull()
    expect(['risk', 'deal']).toContain(classified?.kind)
  })

  it('surfaces 002860 from mocked East Money keyword search', async () => {
    const text = await execScanCorporateEvents(depsWith(async () => eastMoneyResponse([
      { date: '2026-09-21 19:20:00', title: XINGSHUAIER_TELEGRAPH, content: '股票停牌', mediaName: '财联社' },
    ])))
    expect(text).toContain('002860')
    expect(text).toContain('星帅尔')
    expect(text).toContain('不是实盘')
  })
})
