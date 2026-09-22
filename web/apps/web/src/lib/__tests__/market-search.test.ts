import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetStockSearchCacheForTest, resolveStockQuery, searchStocks } from '../market-search'

const DATA: Record<string, unknown[]> = {
  '/market-data/stock_list_cache.json': [
    { code: '601318', name: '中国平安' },
    { code: '300750', name: '宁德时代' },
  ],
  '/market-data/etf_cn_meta.json': [
    { symbol: '513100.SH', code: '513100', name: '纳指ETF', market: 'cn', asset_type: 'etf' },
  ],
  '/market-data/us_meta.json': [
    { symbol: 'AAPL.US', code: 'AAPL', name: '', market: 'us', asset_type: 'stock' },
  ],
  '/market-data/hk_meta.json': [
    { symbol: '00700.HK', code: '00700', name: '', market: 'hk', asset_type: 'stock' },
  ],
  '/market-data/aliases.json': [
    { symbol: 'AAPL.US', name: 'Apple', aliases: ['苹果', '苹果公司'], market: 'us', asset_type: 'stock' },
    { symbol: '00700.HK', name: 'Tencent', aliases: ['腾讯', '腾讯控股'], market: 'hk', asset_type: 'stock' },
  ],
}

beforeEach(() => {
  resetStockSearchCacheForTest()
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({
    ok: true,
    json: () => Promise.resolve(DATA[url] || []),
  })))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('market search', () => {
  it('matches A-share names', async () => {
    const rows = await searchStocks('中国平', 3)
    expect(rows[0]?.analysisCode).toBe('601318')
    expect(rows[0]?.name).toBe('中国平安')
  })

  it('resolves US aliases to TickFlow symbols', async () => {
    const row = await resolveStockQuery('苹果')
    expect(row?.analysisCode).toBe('AAPL.US')
    expect(row?.name).toBe('Apple')
  })

  it('resolves HK codes and aliases', async () => {
    const codeRow = await resolveStockQuery('00700')
    const aliasRow = await resolveStockQuery('腾讯')
    expect(codeRow?.analysisCode).toBe('00700.HK')
    expect(aliasRow?.analysisCode).toBe('00700.HK')
  })

  it('revalidates market files instead of pinning the first response', async () => {
    await searchStocks('00700', 1)
    expect(fetch).toHaveBeenCalledWith('/market-data/hk_meta.json', { cache: 'no-cache' })
  })

  it('keeps CN ETF analysis code as six digits', async () => {
    const row = await resolveStockQuery('纳指')
    expect(row?.analysisCode).toBe('513100')
    expect(row?.assetType).toBe('etf')
  })
})
