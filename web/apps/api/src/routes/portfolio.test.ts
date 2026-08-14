import { describe, expect, it, vi } from 'vitest'
import {
  MISSING_BUY_DT_ERROR,
  normalizeBuyDate,
  parsePortfolioInput,
  positionWriteRecord,
  savePortfolio,
  savePosition,
} from './portfolio'

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

  it('rejects a buy_dt that is not a real calendar date', () => {
    const result = parsePortfolioInput({
      free_cash: 0,
      positions: [{ code: '600519', name: '贵州茅台', shares: 100, cost_price: 1500, buy_dt: '2026-13-40' }],
    })
    expect(result).toHaveProperty('error', 'Invalid portfolio')
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

function mockPortfolioClient(options: { updateRows?: unknown[] }) {
  const insert = vi.fn().mockResolvedValue({ error: null })
  const update = vi.fn()
  const eq = vi.fn()
  const select = vi.fn().mockResolvedValue({ data: options.updateRows ?? [], error: null })
  const chain: Record<string, unknown> = {}
  chain.update = (...args: unknown[]) => {
    update(...args)
    return chain
  }
  chain.eq = (...args: unknown[]) => {
    eq(...args)
    return chain
  }
  chain.select = select
  chain.insert = insert
  const from = vi.fn().mockReturnValue(chain)
  return { supabase: { from } as never, insert, update }
}

describe('savePosition', () => {
  const position = {
    code: '300390',
    name: '天华新能',
    shares: 200,
    cost_price: 64.9,
    buy_dt: null as string | null,
  }

  it('does not insert when update matches no rows', async () => {
    const { supabase, insert } = mockPortfolioClient({ updateRows: [] })
    const error = await savePosition(supabase, 'USER_LIVE:u', position, 'update')
    expect(error).toContain('无法 update')
    expect(insert).not.toHaveBeenCalled()
  })

  it('does not insert an add without buy_dt', async () => {
    const { supabase, insert } = mockPortfolioClient({ updateRows: [] })
    const error = await savePosition(supabase, 'USER_LIVE:u', position, 'add')
    expect(error).toBe(MISSING_BUY_DT_ERROR)
    expect(insert).not.toHaveBeenCalled()
  })
})

function mockSavePortfolioClient(existingCodes: string[]) {
  const deleted: string[] = []
  const inserted: unknown[] = []
  const cashUpdates: unknown[] = []

  const supabase = {
    from(table: string) {
      if (table === 'portfolio_positions') {
        return {
          select: () => ({
            eq: async () => ({ data: existingCodes.map((code) => ({ code })), error: null }),
          }),
          insert: async (row: unknown) => {
            inserted.push(row)
            return { error: null }
          },
          update: () => ({
            eq: () => ({
              eq: () => ({
                select: async () => ({ data: [{ code: 'x' }], error: null }),
              }),
            }),
          }),
          delete: () => ({
            eq: () => ({
              eq: async (_field: string, code: string) => {
                deleted.push(code)
                return { error: null }
              },
            }),
          }),
        }
      }
      return {
        update: (row: unknown) => ({
          eq: () => ({
            select: async () => {
              cashUpdates.push(row)
              return { data: [{ portfolio_id: 'USER_LIVE:u' }], error: null }
            },
          }),
        }),
        insert: async () => ({ error: null }),
      }
    },
  }

  return { supabase: supabase as never, deleted, inserted, cashUpdates }
}

describe('savePortfolio', () => {
  it('does not delete or mutate cash when a replacement add lacks buy_dt', async () => {
    const { supabase, deleted, inserted, cashUpdates } = mockSavePortfolioClient(['AAPL.US'])
    const error = await savePortfolio(supabase, 'u', {
      free_cash: 12_000,
      positions: [{ code: 'TSLA.US', name: 'Tesla', shares: 5, cost_price: 250, buy_dt: null }],
    })

    expect(error).toBe(MISSING_BUY_DT_ERROR)
    expect(deleted).toEqual([])
    expect(inserted).toEqual([])
    expect(cashUpdates).toEqual([])
  })
})
