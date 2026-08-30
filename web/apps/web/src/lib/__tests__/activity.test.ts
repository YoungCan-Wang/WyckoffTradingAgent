import { describe, expect, it, vi } from 'vitest'
import { featureForRoute, recordActivity } from '../activity'

type ActivitySupabase = NonNullable<Parameters<typeof recordActivity>[1]>['supabase']

interface TableResult {
  data?: unknown
  error?: unknown
}

function createMockSupabase(results: Record<string, TableResult> = {}) {
  const inserts: Record<string, unknown[]> = {}
  const upserts: Record<string, unknown[]> = {}
  const from = vi.fn((table: string) => {
    const result = results[table] ?? { data: [] }
    const chain: Record<string, unknown> = {}
    const terminal = () => Promise.resolve({ data: result.data ?? null, error: result.error ?? null })
    for (const method of ['select', 'eq', 'limit']) {
      chain[method] = vi.fn().mockReturnValue(chain)
    }
    chain['insert'] = vi.fn((row: unknown) => {
      inserts[table] = [...(inserts[table] ?? []), row]
      return terminal()
    })
    chain['upsert'] = vi.fn((row: unknown) => {
      upserts[table] = [...(upserts[table] ?? []), row]
      return terminal()
    })
    chain['then'] = (resolve: (value: unknown) => void) => resolve({ data: result.data ?? null, error: result.error ?? null })
    return chain
  })
  return { supabase: { from }, inserts, upserts }
}

describe('featureForRoute', () => {
  it('maps app routes to compact feature keys', () => {
    expect(featureForRoute('/')).toBe('chat')
    expect(featureForRoute('/battle?x=1')).toBe('battle')
    expect(featureForRoute('/membership#capability-boundary')).toBe('membership')
  })
})

describe('recordActivity', () => {
  it('writes raw event and updates daily rollup', async () => {
    const { supabase, inserts, upserts } = createMockSupabase({
      analytics_excluded_users: { data: [] },
      user_daily_activity: {
        data: [{
          event_count: 2,
          session_count: 1,
          sources: ['web'],
          feature_counts: { chat: 2 },
          first_seen_at: '2026-05-27T01:00:00.000Z',
        }],
      },
    })

    const result = await recordActivity(
      { userId: '00000000-0000-0000-0000-000000000001', route: '/analysis?code=000001' },
      {
        supabase: supabase as unknown as ActivitySupabase,
        now: () => new Date('2026-05-27T10:00:00.000Z'),
        eventId: 'evt-1',
        sessionId: 'sess-1',
        countSession: true,
      },
    )

    expect(result).toEqual({ eventWritten: true, dailyWritten: true })
    const eventRows = inserts['user_activity_events'] ?? []
    const dailyRows = upserts['user_daily_activity'] ?? []
    expect(eventRows).toHaveLength(1)
    expect(dailyRows).toHaveLength(1)
    expect(eventRows[0]).toMatchObject({
      event_id: 'evt-1',
      session_id: 'sess-1',
      event_name: 'page_view',
      feature: 'analysis',
      route: '/analysis?code=000001',
    })
    expect(dailyRows[0]).toMatchObject({
      activity_date: '2026-05-27',
      event_count: 3,
      session_count: 2,
      sources: ['web'],
      feature_counts: { chat: 2, analysis: 1 },
    })
  })

  it('skips users excluded from analytics', async () => {
    const { supabase, inserts, upserts } = createMockSupabase({
      analytics_excluded_users: { data: [{ user_id: '00000000-0000-0000-0000-000000000001' }] },
    })

    const result = await recordActivity(
      { userId: '00000000-0000-0000-0000-000000000001', route: '/chat' },
      { supabase: supabase as unknown as ActivitySupabase, eventId: 'evt-1', sessionId: 'sess-1' },
    )

    expect(result).toEqual({ eventWritten: false, dailyWritten: false, skipped: 'excluded' })
    expect(inserts['user_activity_events']).toBeUndefined()
    expect(upserts['user_daily_activity']).toBeUndefined()
  })
})
