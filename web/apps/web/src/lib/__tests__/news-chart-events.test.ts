import { describe, expect, it } from 'vitest'
import { classifyHeadline, handleNewsEventsRequest, selectNewsChartEvents, snapToSession } from '@wyckoff/shared'

describe('news chart overlay', () => {
  it('keeps shareholding news and drops limit-up noise', () => {
    const hit = classifyHeadline('中际旭创拟超17亿元入股中石科技')
    expect(hit?.kind).toBe('deal')
    expect(hit?.sentiment).toBe('bullish')
    expect(classifyHeadline('300684，20cm三连板！')).toBeNull()
    expect(classifyHeadline('20%涨停！万亿巨头17亿重仓杀入', '拟入股中石科技')).toBeNull()
    expect(classifyHeadline('万亿龙头入股！300684，20cm两连板！')).toBeNull()
    expect(classifyHeadline('净利同比增长10.92倍！盘后公告集锦')).toBeNull()
  })

  it('snaps weekend headlines to the next session and keeps one event per day', () => {
    const events = selectNewsChartEvents(
      [
        { title: '中际旭创拟超17亿元入股中石科技', published_at: '2026-08-15 22:38:00' },
        { title: '公司收到证监会立案调查通知', published_at: '2026-08-17 10:00:00' },
      ],
      '2026-08-01',
      '2026-08-20',
      ['2026-08-14', '2026-08-17', '2026-08-18'],
    )
    expect(events.map((event) => event.kind)).toEqual(['regulatory'])
    expect(snapToSession('2026-08-16', ['2026-08-14', '2026-08-17'])).toBe('2026-08-17')
  })

  it('rejects invalid queries without calling upstream', async () => {
    const response = await handleNewsEventsRequest(
      new Request('http://localhost/api/news-events?code=AAPL&start=2026-08-01&end=2026-08-20'),
      async () => {
        throw new Error('should not fetch')
      },
    )
    expect(response.status).toBe(400)
    expect(await response.json()).toMatchObject({ events: [] })
  })

  it('builds overlay events from a mocked East Money payload', async () => {
    const response = await handleNewsEventsRequest(
      new Request('http://localhost/api/news-events?code=300684&name=%E4%B8%AD%E7%9F%B3%E7%A7%91%E6%8A%80&start=2026-08-01&end=2026-08-20&sessions=2026-08-13,2026-08-18'),
      async () => new Response('jQuery3510({"result":{"cmsArticleWebOld":[{"date":"2026-08-13 22:38:00","title":"中际旭创拟超17亿元入股中石科技","content":"战略入股","code":"abc","mediaName":"财联社"}]}})'),
    )
    expect(response.status).toBe(200)
    const payload = await response.json() as { events: Array<{ kind: string; date: string }> }
    expect(payload.events).toEqual([expect.objectContaining({ kind: 'deal', date: '2026-08-13' })])
  })
})
