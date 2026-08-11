import { describe, expect, it } from 'vitest'
import { dedupeTrackingRows } from '@wyckoff/shared'

describe('dedupeTrackingRows', () => {
  it('fills current_price from tracking when newer signal_pending wins', () => {
    const rows = dedupeTrackingRows([
      {
        code: 600750,
        recommend_date: 20260727,
        recommend_count: 3,
        initial_price: 26.36,
        current_price: 28.1,
        change_pct: 6.6,
        source_type: 'recommendation_tracking',
        is_ai_recommended: false,
      },
      {
        code: '600750',
        recommend_date: 20260803,
        recommend_count: 1,
        initial_price: 27.0,
        current_price: null,
        change_pct: null,
        source_type: 'signal_pending',
        is_ai_recommended: false,
      },
    ])

    expect(rows).toHaveLength(1)
    expect(rows[0]?.recommend_date).toBe(20260803)
    expect(rows[0]?.source_type).toBe('signal_pending')
    expect(rows[0]?.initial_price).toBe(26.36)
    expect(rows[0]?.current_price).toBe(28.1)
    expect(rows[0]?.change_pct).toBe(6.6)
    expect(rows[0]?.recommend_count).toBe(3)
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
  })
})
