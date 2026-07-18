import { describe, expect, it } from 'vitest'
import { formatMarketWatchContext, readFreshMarketWatchSnapshot, selectMarketWatchCodes, type MarketWatchSnapshot } from '@wyckoff/shared'

const requestedCodes = ['000001', 'AAPL.US']
const fetchedAt = '2026-07-18T10:00:00.000Z'

function snapshot(): MarketWatchSnapshot {
  return {
    state: 'ready',
    source: 'tickflow',
    requestedCodes,
    fetchedAt,
    fromCache: false,
    quotes: [
      { requestedCode: '000001', symbol: '000001.SZ', price: 10, changePct: 1.2, previousClose: 9.88, volume: 100, asOf: fetchedAt },
      { requestedCode: 'AAPL.US', symbol: 'AAPL.US', price: 200, changePct: -0.5, previousClose: 201, volume: 200, asOf: fetchedAt },
    ],
  }
}

describe('market watch cache validation', () => {
  it('accepts a fresh snapshot for the same basket', () => {
    const result = readFreshMarketWatchSnapshot(snapshot(), requestedCodes, Date.parse(fetchedAt) + 30_000)
    expect(result?.fromCache).toBe(true)
    expect(result?.requestedCodes).toEqual(requestedCodes)
  })

  it('rejects stale or mismatched snapshots', () => {
    expect(readFreshMarketWatchSnapshot(snapshot(), requestedCodes, Date.parse(fetchedAt) + 45_001)).toBeNull()
    expect(readFreshMarketWatchSnapshot(snapshot(), ['000001'], Date.parse(fetchedAt) + 1_000)).toBeNull()
  })

  it('injects only relevant quotes unless the user asks for a reading-basket review', () => {
    expect(selectMarketWatchCodes(requestedCodes, '重点读一下 AAPL.US')).toEqual(['AAPL.US'])
    expect(selectMarketWatchCodes([{ code: '603039', name: '泛微网络' }], '看看泛微网络')).toEqual(['603039'])
    expect(selectMarketWatchCodes(requestedCodes, '复盘我的观察篮')).toEqual(requestedCodes)
    expect(selectMarketWatchCodes(requestedCodes, '解释一下威科夫的弹簧')).toEqual([])
    const full = formatMarketWatchContext({ ...snapshot(), fromCache: true }, ['AAPL.US'])
    expect(full).toContain('AAPL.US')
    expect(full).not.toContain('000001 |')
    expect(full).toContain('浏览器本地缓存')
  })
})
