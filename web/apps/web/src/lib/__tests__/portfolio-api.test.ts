import { describe, expect, it, vi } from 'vitest'
import { requestPortfolio } from '../portfolio-api'

function response(body: unknown, init?: ResponseInit): Response {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), init)
}

describe('requestPortfolio', () => {
  it('returns a validated portfolio', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ free_cash: 1200, positions: [] }))

    await expect(requestPortfolio('GET', 'token', undefined, fetcher)).resolves.toEqual({
      free_cash: 1200,
      positions: [],
    })
    expect(fetcher).toHaveBeenCalledWith('http://127.0.0.1:8787/api/portfolio', expect.objectContaining({
      method: 'GET',
      headers: { Authorization: 'Bearer token' },
    }))
  })

  it('normalizes an empty buy date from stored positions', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({
      free_cash: 0,
      positions: [{ code: '600611', name: '大众交通', shares: 1400, cost_price: 3.854, buy_dt: '' }],
    }))

    await expect(requestPortfolio('GET', 'token', undefined, fetcher)).resolves.toEqual({
      free_cash: 0,
      positions: [{ code: '600611', name: '大众交通', shares: 1400, cost_price: 3.854, buy_dt: null }],
    })
  })

  it('normalizes the compact dates returned by the production portfolio API', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({
      free_cash: 40000,
      positions: [
        { code: '600611', name: '大众交通', shares: 1400, cost_price: 3.854, buy_dt: '20260703' },
        { code: '603995', name: '甬金股份', shares: 500, cost_price: 23.761, buy_dt: '20260703' },
        { code: '600378', name: '昊华科技', shares: 200, cost_price: 81.026, buy_dt: '20260701' },
        { code: '603661', name: '恒林股份', shares: 600, cost_price: 40.019, buy_dt: '20260630' },
      ],
    }))

    const portfolio = await requestPortfolio('GET', 'token', undefined, fetcher)

    expect(portfolio.free_cash).toBe(40000)
    expect(portfolio.positions.map((position) => position.buy_dt)).toEqual([
      '2026-07-03', '2026-07-03', '2026-07-01', '2026-06-30',
    ])
  })

  it('rejects the Pages SPA fallback instead of returning an empty object', async () => {
    const fetcher = vi.fn().mockResolvedValue(response('<!doctype html><html></html>', {
      headers: { 'content-type': 'text/html' },
    }))

    await expect(requestPortfolio('GET', 'token', undefined, fetcher)).rejects.toThrow('持仓服务返回数据不完整')
  })

  it('rejects successful responses with missing positions', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ free_cash: 0 }))

    await expect(requestPortfolio('GET', 'token', undefined, fetcher)).rejects.toThrow('持仓服务返回数据不完整')
  })

  it('surfaces API errors', async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ error: 'Whitelist required' }, { status: 403 }))

    await expect(requestPortfolio('GET', 'token', undefined, fetcher)).rejects.toThrow('Whitelist required')
  })
})
