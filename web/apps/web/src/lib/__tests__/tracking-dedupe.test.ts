import { describe, expect, it } from 'vitest'
import {
  countTrackingOccurrences,
  dedupeTrackingRows,
  eventChangePct,
  groupTrackingByCode,
  hasCompleteTrackingWindow,
  latestTrackingDates,
} from '@wyckoff/shared'

describe('dedupeTrackingRows', () => {
  it('keeps different dates as separate events', () => {
    const rows = dedupeTrackingRows([
      {
        code: 600750,
        recommend_date: 20260727,
        recommend_count: 3,
        initial_price: 26.36,
        current_price: 28.1,
        change_pct: 0,
        source_type: 'recommendation_tracking',
        is_ai_recommended: false,
      },
      {
        code: '600750',
        recommend_date: 20260803,
        recommend_count: 1,
        initial_price: 27.0,
        current_price: 28.1,
        change_pct: null,
        source_type: 'signal_pending',
        is_ai_recommended: false,
      },
    ])

    expect(rows).toHaveLength(2)
    const later = rows.find((row) => row.recommend_date === 20260803)
    expect(later?.source_type).toBe('signal_pending')
    expect(later?.initial_price).toBe(27.0)
    expect(later?.change_pct).toBe(4.07)
  })

  it('keeps tracking row on same date over signal_pending', () => {
    const rows = dedupeTrackingRows([
      {
        code: 1,
        recommend_date: 20260803,
        initial_price: 10,
        current_price: null,
        source_type: 'signal_pending',
      },
      {
        code: 1,
        recommend_date: 20260803,
        initial_price: 9.5,
        current_price: 11,
        change_pct: 15.79,
        source_type: 'recommendation_tracking',
        recommend_count: 2,
      },
    ])

    expect(rows[0]?.source_type).toBe('recommendation_tracking')
    expect(rows[0]?.current_price).toBe(11)
    expect(rows[0]?.recommend_count).toBe(2)
    expect(rows[0]?.change_pct).toBe(15.79)
  })

  it('recomputes change from the event prices instead of keeping a stale zero', () => {
    expect(eventChangePct(294.2, 327.07)).toBe(11.17)
  })

  it('groups events by code for first-to-now stats', () => {
    const groups = groupTrackingByCode([
      { code: '688519', recommend_date: 20260827, initial_price: 294.2, current_price: 327.07 },
      { code: '688519', recommend_date: 20260911, initial_price: 327.07, current_price: 327.07 },
    ])

    expect(groups).toHaveLength(1)
    expect(groups[0]?.eventCount).toBe(2)
    expect(groups[0]?.sinceFirstPct).toBe(11.17)
  })
})

describe('tracking window metrics', () => {
  it('counts each stock and review date once across duplicate sources', () => {
    const rows = [
      { code: 'AAPL', recommend_date: 20260811, source_type: 'recommendation_tracking' },
      { code: 'AAPL', recommend_date: '2026-08-11', source_type: 'signal_pending' },
      { code: 'AAPL', recommend_date: 20260810, source_type: 'recommendation_tracking' },
      { code: 'MSFT', recommend_date: 20260811, source_type: 'recommendation_tracking' },
    ]

    expect(countTrackingOccurrences(rows)).toBe(3)
  })

  it('stops paging only after crossing the complete retention boundary', () => {
    const thirtyDates = Array.from({ length: 30 }, (_, index) => ({
      code: `S${index}`,
      recommend_date: 20260830 - index,
    }))

    expect(latestTrackingDates(thirtyDates, 30)).toHaveLength(30)
    expect(hasCompleteTrackingWindow(thirtyDates, 30)).toBe(false)
    expect(
      hasCompleteTrackingWindow(
        [...thirtyDates, { code: 'OLDER', recommend_date: 20260731 }],
        30,
      ),
    ).toBe(true)
  })
})
