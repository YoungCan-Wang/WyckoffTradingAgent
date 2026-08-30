import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchKlineViaTickFlow } from '../kline'

describe('kline TickFlow parsing', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('normalizes Unix second timestamps into trading dates', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        data: {
          '603039.SH': {
            timestamp: [1704067200, 1704153600],
            open: [10, 11],
            high: [11, 12],
            low: [9, 10],
            close: [10.5, 11.5],
            volume: [1000, 1200],
          },
        },
      }),
    }))

    const rows = await fetchKlineViaTickFlow('603039', 'tf-test')

    expect(rows.map((row) => row.date)).toEqual(['2024-01-01', '2024-01-02'])
    expect(rows[0]).toMatchObject({ open: 10, close: 10.5, volume: 1000 })
  })
})
