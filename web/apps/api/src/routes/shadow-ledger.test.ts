import { beforeEach, describe, expect, it, vi } from 'vitest'
import { isActivePlanetMember } from '../middleware/planet-membership'
import { createAdminSupabase, createUserSupabase, resolveUserId } from '../middleware/auth'
import { SHADOW_ACCOUNT_ID, showcaseLeaksSensitiveFields } from '../services/shadow-ledger'
import { shadowLedgerRoutes } from './shadow-ledger'

vi.mock('../middleware/planet-membership', () => ({ isActivePlanetMember: vi.fn() }))
vi.mock('../middleware/auth', () => ({
  createAdminSupabase: vi.fn(),
  createUserSupabase: vi.fn(),
  resolveUserId: vi.fn(),
}))

const membership = vi.mocked(isActivePlanetMember)
const admin = vi.mocked(createAdminSupabase)
const userClient = vi.mocked(createUserSupabase)
const userId = vi.mocked(resolveUserId)

const env = {
  SUPABASE_URL: 'https://example.supabase.co',
  SUPABASE_ANON_KEY: 'anon',
  SUPABASE_SERVICE_ROLE_KEY: 'service-role',
}

function fixtureTables() {
  return {
    shadow_account: [{
      account_id: SHADOW_ACCOUNT_ID,
      cash: 60000,
      equity: 104000,
      market_value: 44000,
      initial_capital: 100000,
      as_of: '2026-09-18',
    }],
    shadow_nav_daily: [
      { as_of: '2026-09-17', cash: 60000, market_value: 46000, equity: 106000, pnl_day: 6000, pnl_total: 6000 },
      { as_of: '2026-09-18', cash: 60000, market_value: 44000, equity: 104000, pnl_day: -2000, pnl_total: 4000 },
    ],
    shadow_positions: [
      { code: '600373', name: '中文传媒', shares: 1000, avg_cost: 10, last_mark: 11 },
    ],
    shadow_events: [
      {
        as_of: '2026-09-18',
        code: '600373',
        name: '中文传媒',
        event_type: 'buy',
        price: 10,
        qty: 1000,
        reason: '漏斗买许可',
        fees: { commission: 3 },
        payload: { status: 'filled' },
      },
    ],
  }
}

function mockAdmin(tables = fixtureTables()) {
  const queried: string[] = []
  admin.mockReturnValue({
    from(table: string) {
      queried.push(table)
      const rows = tables[table as keyof typeof tables] ?? []
      const chain: Record<string, unknown> = {}
      const self = () => chain
      chain.select = self
      chain.eq = self
      chain.gt = self
      chain.order = self
      chain.maybeSingle = () => Promise.resolve({ data: rows[0] ?? null, error: null })
      chain.then = (
        resolve: (value: { data: unknown[]; error: null }) => unknown,
        reject?: (reason: unknown) => unknown,
      ) => Promise.resolve({ data: rows, error: null }).then(resolve, reject)
      return chain
    },
  } as never)
  return queried
}

async function getLedger(headers?: Record<string, string>, path = '/') {
  return shadowLedgerRoutes.request(path, { headers }, env)
}

describe('shadow ledger route membership gate', () => {
  beforeEach(() => {
    membership.mockReset()
    admin.mockReset()
    userClient.mockReset()
    userId.mockReset()
    userClient.mockReturnValue({} as never)
  })

  it('returns a showcase DTO without login and never queries live tables', async () => {
    const queried = mockAdmin()
    const response = await getLedger()
    const body = await response.json() as { tier: string; showcase: unknown; ledger?: unknown }

    expect(response.status).toBe(200)
    expect(body.tier).toBe('showcase')
    expect(body.ledger).toBeUndefined()
    expect(showcaseLeaksSensitiveFields(body as never)).toEqual([])
    expect(userId).not.toHaveBeenCalled()
    expect(membership).not.toHaveBeenCalled()
    expect(queried).toEqual(['shadow_account', 'shadow_nav_daily', 'shadow_positions', 'shadow_events'])
    expect(queried.join(',')).not.toMatch(/portfolio|daily_nav|trade_orders/)
  })

  it('keeps logged-in non-members on the showcase DTO', async () => {
    mockAdmin()
    userId.mockResolvedValue('user-1')
    membership.mockResolvedValue(false)
    const response = await getLedger({ Authorization: 'Bearer member-token' })
    const body = await response.json() as { tier: string; ledger?: unknown }

    expect(response.status).toBe(200)
    expect(body.tier).toBe('showcase')
    expect(body.ledger).toBeUndefined()
    expect(membership).toHaveBeenCalled()
  })

  it('gives active planet members the daily ledger and open-position net pnl', async () => {
    mockAdmin()
    userId.mockResolvedValue('user-1')
    membership.mockResolvedValue(true)
    const response = await getLedger({ Authorization: 'Bearer member-token' }, '/?asOf=2026-09-18')
    const body = await response.json() as {
      tier: string
      ledger?: { events: Array<{ code: string }>; positions: Array<{ netPnlAmount: number; code: string }> }
    }

    expect(response.status).toBe(200)
    expect(body.tier).toBe('full')
    expect(body.ledger?.events.map((row) => row.code)).toEqual(['600373'])
    expect(body.ledger?.positions[0]).toMatchObject({ code: '600373', netPnlAmount: 1000 })
  })
})
