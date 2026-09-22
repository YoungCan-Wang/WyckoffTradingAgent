import { describe, expect, it } from 'vitest'
import {
  SHADOW_ACCOUNT_ID,
  SHADOW_SHOWCASE_FORBIDDEN_KEYS,
  assertShadowAccount,
  buildShadowLedgerPayload,
  crudeWinRate,
  maxDrawdownPct,
  openPositionNetPnl,
  sectorTagFromName,
  shadowTable,
  showcaseLeaksSensitiveFields,
  type ShadowSnapshot,
} from './shadow-ledger'

const snapshot: ShadowSnapshot = {
  account: {
    account_id: SHADOW_ACCOUNT_ID,
    initial_capital: 100000,
    equity: 104000,
    as_of: '2026-09-18',
  },
  navDaily: [
    { as_of: '2026-09-16', cash: 80000, market_value: 20000, equity: 100000, pnl_day: 0, pnl_total: 0 },
    { as_of: '2026-09-17', cash: 60000, market_value: 46000, equity: 106000, pnl_day: 6000, pnl_total: 6000 },
    { as_of: '2026-09-18', cash: 60000, market_value: 44000, equity: 104000, pnl_day: -2000, pnl_total: 4000 },
  ],
  positions: [
    { code: '600373', name: '中文传媒', shares: 1000, avg_cost: 10, last_mark: 11 },
    { code: '000792', name: '盐湖股份', shares: 0, avg_cost: 20, last_mark: 21 },
    { code: '002250', name: '联化科技化工', shares: 500, avg_cost: 8, last_mark: 7.2 },
  ],
  events: [
    {
      as_of: '2026-09-17',
      code: '600373',
      name: '中文传媒',
      event_type: 'buy',
      price: 10,
      qty: 1000,
      reason: '漏斗买许可',
      payload: { status: 'filled' },
    },
    {
      as_of: '2026-09-18',
      code: '002250',
      name: '联化科技化工',
      event_type: 'sell',
      price: 9,
      qty: 400,
      reason: '止损',
      payload: { status: 'filled' },
    },
  ],
}

describe('shadow ledger assembly', () => {
  it('keeps showcase payload free of code, name, price, qty and stop fields', () => {
    const payload = buildShadowLedgerPayload(snapshot, 'showcase')
    expect(payload.tier).toBe('showcase')
    expect(payload.ledger).toBeUndefined()
    expect(payload.disclaimer).toContain('纸面账')
    expect(payload.showcase.openPositionCount).toBe(2)
    expect(payload.showcase.sectorTags).toEqual(['传媒', '化工'])
    expect(payload.showcase.periodPnlAmount).toBe(4000)
    expect(payload.showcase.periodPnlPct).toBe(4)
    expect(showcaseLeaksSensitiveFields(payload)).toEqual([])
    for (const key of SHADOW_SHOWCASE_FORBIDDEN_KEYS) {
      expect(JSON.stringify(payload.showcase)).not.toContain(`"${key}"`)
    }
  })

  it('gives members daily nav, filtered events and Feishu-consistent open PnL', () => {
    const payload = buildShadowLedgerPayload(snapshot, 'full', '2026-09-18')
    expect(payload.tier).toBe('full')
    expect(payload.ledger?.navDaily).toHaveLength(3)
    expect(payload.ledger?.events).toEqual([
      expect.objectContaining({
        asOf: '2026-09-18',
        code: '002250',
        name: '联化科技化工',
        eventType: 'sell',
        price: 9,
        qty: 400,
        reason: '止损',
      }),
    ])
    expect(payload.ledger?.positions).toEqual([
      {
        code: '002250',
        name: '联化科技化工',
        shares: 500,
        avgCost: 8,
        lastMark: 7.2,
        netPnlAmount: 500 * 7.2 - 500 * 8,
        netPnlPct: ((500 * 7.2 - 500 * 8) / (500 * 8)) * 100,
      },
      {
        code: '600373',
        name: '中文传媒',
        shares: 1000,
        avgCost: 10,
        lastMark: 11,
        netPnlAmount: 1000,
        netPnlPct: 10,
      },
    ])
  })

  it('rejects live accounts and live tables', () => {
    expect(() => assertShadowAccount('USER_LIVE:abc')).toThrow(/USER_SHADOW/)
    expect(() => shadowTable({ from: () => { throw new Error('should not query') } }, 'portfolios')).toThrow(/禁止访问/)
    expect(() => shadowTable({ from: () => { throw new Error('should not query') } }, 'daily_nav')).toThrow(/禁止访问/)
  })
})

describe('shadow ledger metrics', () => {
  it('maps sector keywords and falls back to 其他', () => {
    expect(sectorTagFromName('湖北宜化')).toBe('其他')
    expect(sectorTagFromName('扬农化工')).toBe('化工')
    expect(sectorTagFromName('云天化化肥')).toBe('化肥')
    expect(sectorTagFromName('分众传媒')).toBe('传媒')
  })

  it('uses the Feishu open-position formula shares*last_mark - shares*avg_cost', () => {
    expect(openPositionNetPnl(100, 10, 11)).toEqual({ amount: 100, pct: 10 })
    expect(openPositionNetPnl(100, 0, 11)).toEqual({ amount: 1100, pct: null })
  })

  it('computes max drawdown from the equity peak', () => {
    expect(maxDrawdownPct([100, 110, 99])).toBeCloseTo((110 - 99) / 110 * 100)
  })

  it('omits crude win rate when there is no closed sell', () => {
    expect(crudeWinRate(snapshot.events)).toEqual({ pct: null, note: '平仓样本不足，未计算粗胜率' })
    const withRoundTrip = [
      { as_of: '2026-09-16', code: '000001', event_type: 'buy', price: 10, qty: 100, payload: { status: 'filled' } },
      { as_of: '2026-09-17', code: '000001', event_type: 'sell', price: 11, qty: 100, payload: { status: 'filled' } },
      { as_of: '2026-09-18', code: '000002', event_type: 'buy', price: 8, qty: 100, payload: { status: 'filled' } },
      { as_of: '2026-09-19', code: '000002', event_type: 'sell', price: 7, qty: 100, payload: { status: 'filled' } },
    ]
    expect(crudeWinRate(withRoundTrip).pct).toBe(50)
    expect(crudeWinRate(withRoundTrip).note).toBeNull()
  })
})
