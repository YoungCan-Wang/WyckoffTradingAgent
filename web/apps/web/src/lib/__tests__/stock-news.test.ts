import { describe, expect, it } from 'vitest'
import { execStockNews, selectStockNewsHeadlines, type ToolDeps } from '@wyckoff/shared'

/** execStockNews 只用到 deps.fetch,其余字段给个空壳即可。 */
function depsWith(fetchImpl: typeof globalThis.fetch): ToolDeps {
  return { supabase: null as never, fetch: fetchImpl, generateText: null as never }
}

function eastMoneyResponse(articles: Array<Record<string, string>>): Response {
  return new Response(`jQuery3510(${JSON.stringify({ result: { cmsArticleWebOld: articles } })})`)
}

describe('selectStockNewsHeadlines', () => {
  it('保留归不了类的条目 —— 核证不该只看那五类事件', () => {
    const rows = selectStockNewsHeadlines([
      { title: '中石科技中标某车企年度采购项目', published_at: '2026-08-20 09:00:00' },
      { title: '中石科技完成董事会换届', published_at: '2026-08-19 09:00:00' },
    ], 12, '', '中石科技')
    expect(rows.map((row) => row.kind)).toEqual(['deal', null])
    expect(rows).toHaveLength(2)
  })

  it('过滤涨停板与盘后集锦这类没有事件内核的标题', () => {
    const rows = selectStockNewsHeadlines([
      { title: '300684，20cm三连板！', published_at: '2026-08-20 09:00:00' },
      { title: '净利同比增长10.92倍！盘后公告集锦', published_at: '2026-08-19 09:00:00' },
      { title: '公司收到证监会立案调查通知', published_at: '2026-08-18 09:00:00' },
    ], 12)
    expect(rows.map((row) => row.title)).toEqual(['公司收到证监会立案调查通知'])
  })

  it('按发布日倒序,同标题只留一条', () => {
    const rows = selectStockNewsHeadlines([
      { title: '公司披露业绩预增公告', published_at: '2026-08-10 09:00:00' },
      { title: '公司披露业绩预增公告', published_at: '2026-08-10 15:00:00' },
      { title: '大股东拟减持不超过2%', published_at: '2026-08-21 09:00:00' },
    ], 12)
    expect(rows.map((row) => row.date)).toEqual(['2026-08-21', '2026-08-10'])
  })

  it('同一天可以留两条 —— 与作图的一天一个不同', () => {
    const rows = selectStockNewsHeadlines([
      { title: '公司收到证监会问询函', published_at: '2026-08-20 09:00:00' },
      { title: '控股股东增持公司股份', published_at: '2026-08-20 15:00:00' },
    ], 12)
    expect(rows).toHaveLength(2)
    expect(new Set(rows.map((row) => row.date))).toEqual(new Set(['2026-08-20']))
  })

  it('保留原始发布日,不贴到交易日', () => {
    const rows = selectStockNewsHeadlines([
      { title: '公司披露中报,净利扭亏', published_at: '2026-08-15 22:38:00' },
    ], 12)
    expect(rows[0]?.date).toBe('2026-08-15')
  })

  it('limit 为 0 时返回空数组', () => {
    const rows = selectStockNewsHeadlines([
      { title: '公司收到证监会立案调查通知', published_at: '2026-08-18 09:00:00' },
    ], 0)
    expect(rows).toEqual([])
  })
})

describe('execStockNews', () => {
  it('拒绝非 A 股代码,不打上游', async () => {
    const text = await execStockNews(depsWith(() => { throw new Error('should not fetch') }), 'AAPL.US', null, 12)
    expect(text).toContain('不是 A 股 6 位代码')
  })

  it('不足 6 位的代码先补零再判定', async () => {
    const text = await execStockNews(depsWith(async () => eastMoneyResponse([
      { date: '2026-08-20 09:00:00', title: '平安银行披露业绩预增公告', content: '预增', mediaName: '财联社', code: 'abc' },
    ])), '1', '平安银行', 12)
    expect(text).toContain('000001')
    expect(text).toContain('业绩预增')
  })

  it('输出带核证口径,并说明发布日未贴交易日', async () => {
    const text = await execStockNews(depsWith(async () => eastMoneyResponse([
      { date: '2026-08-20 09:00:00', title: '中石科技收到证监会立案调查通知', content: '立案', mediaName: '财联社', code: 'abc' },
    ])), '300684', '中石科技', 12)
    expect(text).toContain('用途：核证量价结构判断')
    expect(text).toContain('不要反过来用消息推结构')
    expect(text).toContain('未贴到交易日')
    expect(text).toContain('[监管/偏空]')
  })

  it('检索为空时说清「没命中 ≠ 无事发生」', async () => {
    const text = await execStockNews(depsWith(async () => eastMoneyResponse([])), '300684', '中石科技', 12)
    expect(text).toContain('不等于确实无事发生')
  })

  it('上游报错时不谎称没有消息', async () => {
    const text = await execStockNews(depsWith(async () => { throw new Error('upstream down') }), '300684', '中石科技', 12)
    expect(text).toContain('消息源暂时不可用')
    expect(text).toContain('不要据此断定')
  })

  it('limit 收敛到 20 上限', async () => {
    const articles = Array.from({ length: 20 }, (_, index) => ({
      date: `2026-08-${String(index + 1).padStart(2, '0')} 09:00:00`,
      title: `中石科技第${index + 1}次披露业绩预增公告`,
      content: '预增',
      mediaName: '财联社',
      code: 'abc',
    }))
    const text = await execStockNews(depsWith(async () => eastMoneyResponse(articles)), '300684', '中石科技', 999)
    expect(text).toContain('近期消息 20 条')
  })
})
