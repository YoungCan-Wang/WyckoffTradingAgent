import { describe, expect, it } from 'vitest'
import { MISSING_BUY_DT_ERROR, normalizeBuyDate, parsePortfolioInput, positionWriteRecord } from './portfolio'

describe('portfolio API input', () => {
  it('accepts a valid user portfolio', () => {
    const result = parsePortfolioInput({
      free_cash: 1000,
      positions: [{ code: '600519', name: '贵州茅台', shares: 100, cost_price: 1500, buy_dt: '2026-07-01' }],
    })

    expect('data' in result).toBe(true)
  })

  it('normalizes an empty buy date from legacy rows', () => {
    const result = parsePortfolioInput({
      free_cash: 0,
      positions: [{ code: '600611', name: '大众交通', shares: 1400, cost_price: 3.854, buy_dt: '' }],
    })

    expect(result).toEqual({
      data: {
        free_cash: 0,
        positions: [{ code: '600611', name: '大众交通', shares: 1400, cost_price: 3.854, buy_dt: null }],
      },
    })
  })

  it('normalizes compact buy dates from stored positions', () => {
    expect(normalizeBuyDate('20260703')).toBe('2026-07-03')
    const result = parsePortfolioInput({
      free_cash: 40000,
      positions: [{ code: '603995', name: '甬金股份', shares: 500, cost_price: 23.761, buy_dt: '20260703' }],
    })

    expect(result).toEqual({
      data: {
        free_cash: 40000,
        positions: [{ code: '603995', name: '甬金股份', shares: 500, cost_price: 23.761, buy_dt: '2026-07-03' }],
      },
    })
  })

  it('rejects duplicate symbols and invalid quantities', () => {
    const duplicate = parsePortfolioInput({
      free_cash: 0,
      positions: [
        { code: 'aapl.us', name: null, shares: 1, cost_price: 100, buy_dt: null },
        { code: 'AAPL.US', name: null, shares: 2, cost_price: 110, buy_dt: null },
      ],
    })
    const fractional = parsePortfolioInput({
      free_cash: 0,
      positions: [{ code: '600519', name: null, shares: 1.5, cost_price: 100, buy_dt: null }],
    })

    expect(duplicate).toEqual({ error: 'Duplicate position code' })
    expect(fractional).toHaveProperty('error', 'Invalid portfolio')
  })

  it('normalizes HK/US codes and rejects bare short digits', () => {
    const ok = parsePortfolioInput({
      free_cash: 0,
      positions: [
        { code: '6881.HK', name: '中国银河', shares: 1000, cost_price: 7.68, buy_dt: '2026-08-07' },
        { code: 'aapl', name: 'Apple', shares: 10, cost_price: 200, buy_dt: null },
      ],
    })
    expect(ok).toEqual({
      data: {
        free_cash: 0,
        positions: [
          { code: '06881.HK', name: '中国银河', shares: 1000, cost_price: 7.68, buy_dt: '2026-08-07' },
          { code: 'AAPL.US', name: 'Apple', shares: 10, cost_price: 200, buy_dt: null },
        ],
      },
    })

    const bad = parsePortfolioInput({
      free_cash: 0,
      positions: [{ code: '6881', name: '中国银河', shares: 1000, cost_price: 7.68, buy_dt: null }],
    })
    expect(bad).toMatchObject({ error: expect.stringContaining('Invalid position code') })
  })
})

describe('positionWriteRecord', () => {
  it('omits buy_dt on size/cost edits when the caller did not send a date', () => {
    const record = positionWriteRecord('USER_LIVE:u', {
      code: '000001',
      name: '平安银行',
      shares: 200,
      cost_price: 10.5,
      buy_dt: null,
    })
    expect(record).not.toHaveProperty('buy_dt')
    expect(record).toMatchObject({ code: '000001', shares: 200, cost_price: 10.5 })
  })

  it('keeps an explicit buy date and rejects a new long without one', () => {
    const withDate = positionWriteRecord('USER_LIVE:u', {
      code: '300390',
      name: '天华新能',
      shares: 200,
      cost_price: 64.9,
      buy_dt: '2026-08-12',
    })
    expect(withDate.buy_dt).toBe('2026-08-12')
    const missing = positionWriteRecord('USER_LIVE:u', {
      code: '300390',
      name: '天华新能',
      shares: 200,
      cost_price: 64.9,
      buy_dt: null,
    })
    expect(missing.buy_dt).toBeUndefined()
    expect(MISSING_BUY_DT_ERROR).toContain('buy_dt')
  })
})
