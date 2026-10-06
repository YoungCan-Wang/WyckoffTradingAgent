import { describe, expect, it, vi } from 'vitest'
import { isActivePlanetMember } from '../middleware/planet-membership'
import {
  buildCloudPortfolioTools,
  executeApprovedCloudPortfolioUpdate,
} from './chat-portfolio-tools'

vi.mock('../middleware/planet-membership', () => ({ isActivePlanetMember: vi.fn() }))
vi.mock('@wyckoff/shared', async () => {
  const actual = await vi.importActual<typeof import('@wyckoff/shared')>('@wyckoff/shared')
  return {
    ...actual,
    execExecutePortfolioUpdate: vi.fn(async () => '✅ 已新增 mock'),
  }
})

const membership = vi.mocked(isActivePlanetMember)

function mockDeps() {
  return { supabase: {}, fetch: vi.fn(), generateText: vi.fn() } as never
}

describe('cloud portfolio chat tools', () => {
  it('hides plan/execute tools from non-members', () => {
    expect(buildCloudPortfolioTools(mockDeps(), 'user-1', { enabled: false })).toEqual({})
  })

  it('exposes plan and execute tools to planet members', () => {
    const tools = buildCloudPortfolioTools(mockDeps(), 'user-1', { enabled: true })
    expect(Object.keys(tools).sort()).toEqual(['execute_portfolio_update', 'plan_portfolio_update'])
  })

  it('refuses execute when membership is no longer active', async () => {
    membership.mockResolvedValueOnce(false)
    const result = await executeApprovedCloudPortfolioUpdate(
      mockDeps(),
      'user-1',
      'add',
      '600519',
      '贵州茅台',
      100,
      1800,
      1700,
      '2026-08-12',
    )
    expect(result).toContain('星球会员')
    expect(membership).toHaveBeenCalled()
  })

  it('forwards execute to shared writer when membership is active', async () => {
    membership.mockResolvedValueOnce(true)
    const { execExecutePortfolioUpdate } = await import('@wyckoff/shared')
    const result = await executeApprovedCloudPortfolioUpdate(
      mockDeps(),
      'user-1',
      'add',
      '600519',
      '贵州茅台',
      100,
      1800,
      1700,
      '2026-08-12',
    )
    expect(result).toContain('已新增')
    expect(execExecutePortfolioUpdate).toHaveBeenCalledWith(
      expect.anything(),
      'user-1',
      'add',
      '600519',
      '贵州茅台',
      100,
      1800,
      1700,
      '2026-08-12',
    )
  })
})
