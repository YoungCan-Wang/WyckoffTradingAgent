import { describe, it, expect, vi } from 'vitest'
import type { ToolDeps, KlineRow } from '@wyckoff/shared'
import {
  buildValueAgentDigest,
  buildKlineDigest,
  buildPortfolioWriteRecord,
  execSearchStock,
  execViewPortfolio,
  execMarketOverview,
  execQueryRecommendations,
  execQueryAttribution,
  execExecutePortfolioUpdate,
  execScreenStocks,
  execStrategyDecision,
  execGenerateAiReport,
  execAnalyzeStock,
  execMarketHistory,
  SCREEN_RESULT_OUTPUT_SCHEMA,
  STRATEGY_DECISION_OUTPUT_SCHEMA,
} from '@wyckoff/shared'

function createMockChain(resolvedData: unknown = null, error: unknown = null) {
  const chain: Record<string, unknown> = {}
  const terminal = () => Promise.resolve({ data: resolvedData, error })
  for (const method of ['select', 'eq', 'ilike', 'in', 'order', 'limit', 'delete', 'update']) {
    chain[method] = vi.fn().mockReturnValue(chain)
  }
  chain['insert'] = vi.fn().mockImplementation(terminal)
  chain['single'] = vi.fn().mockImplementation(terminal)
  chain['upsert'] = vi.fn().mockImplementation(terminal)
  // make the chain itself thenable for queries without .single()
  chain['then'] = (resolve: (v: unknown) => void) => resolve({ data: resolvedData, error })
  return chain
}

function createPortfolioWriteDeps(updateRows: unknown[]) {
  const updateChain = createMockChain(updateRows)
  const insertChain = createMockChain(null)
  const mockFrom = vi.fn()
    .mockReturnValueOnce(updateChain)
    .mockReturnValueOnce(insertChain)
  const deps = {
    supabase: { from: mockFrom } as unknown as ToolDeps['supabase'],
    fetch: vi.fn(),
    generateText: vi.fn(),
  } as unknown as ToolDeps
  return { deps, updateChain, insertChain }
}

function createMockDeps(tableData: Record<string, unknown> = {}): ToolDeps {
  const mockFrom = vi.fn().mockImplementation((table: string) => {
    const data = tableData[table] ?? null
    return createMockChain(data)
  })

  return {
    supabase: { from: mockFrom } as unknown as ToolDeps['supabase'],
    fetch: vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve({}) } as Response),
    generateText: vi.fn().mockResolvedValue({ text: 'mocked LLM response' }),
  }
}

function makeKlineRows(n: number, base = 10): KlineRow[] {
  return Array.from({ length: n }, (_, i) => ({
    date: `2024-01-${String(i + 1).padStart(2, '0')}`,
    open: base + i * 0.1,
    high: base + i * 0.1 + 0.5,
    low: base + i * 0.1 - 0.3,
    close: base + i * 0.12,
    volume: 100000 + i * 1000,
  }))
}

describe('buildKlineDigest', () => {
  it('returns placeholder for empty data', () => {
    expect(buildKlineDigest([])).toBe('无可用K线数据')
  })

  it('produces stable output for 5 rows', () => {
    const rows = makeKlineRows(5)
    expect(buildKlineDigest(rows)).toMatchSnapshot()
  })

  it('produces stable output for 20 rows', () => {
    const rows = makeKlineRows(20)
    expect(buildKlineDigest(rows)).toMatchSnapshot()
  })

  it('includes MA50 for 50+ rows', () => {
    const rows = makeKlineRows(60)
    const result = buildKlineDigest(rows)
    expect(result).toContain('MA50=')
  })

  it('includes MA120 for 120+ rows', () => {
    const rows = makeKlineRows(130)
    const result = buildKlineDigest(rows)
    expect(result).toContain('MA120=')
  })
})

describe('buildValueAgentDigest', () => {
  it('adds score signals to the compact value prompt', () => {
    const digest = buildValueAgentDigest({
      symbol: '600519.SH',
      source: 'tickflow',
      metrics: {
        period_end: '2026-03-31',
        roe: 18.2,
        net_income_yoy: 11.8,
        revenue_yoy: 6.5,
        gross_margin: 91.6,
        debt_to_asset_ratio: 21.4,
        operating_cash_to_revenue: 16.2,
      },
    })

    expect(digest).toContain('价值面摘要（来源：TickFlow，报告期：2026-03-31）')
    expect(digest).toContain('ROE=18.20%')
    expect(digest).toContain('价值面评级：稳健')
    expect(digest).toContain('质量信号：')
    expect(digest).toContain('规则版本：value-rules-v2')
    expect(digest).not.toContain('严重风险')
  })

  it('appends a severe-risk warning for distressed fundamentals', () => {
    const digest = buildValueAgentDigest({
      symbol: '000002.SZ',
      source: 'tickflow',
      metrics: {
        period_end: '2024-12-31',
        roe: -5,
        net_income_yoy: -45,
        revenue_yoy: -25,
        debt_to_asset_ratio: 88,
        operating_cash_to_revenue: -3,
      },
    })

    expect(digest).toContain('价值面评级：高危')
    expect(digest).toContain('严重风险：')
  })
})

describe('execSearchStock', () => {
  it('returns not-found message when no results', async () => {
    const deps = createMockDeps({
      recommendation_tracking: [],
      portfolio_positions: [],
    })
    const result = await execSearchStock(deps, 'user1', '999999')
    expect(result).toContain('未找到匹配')
  })

  it('returns formatted stock list with code and name', async () => {
    const stocks = [{ code: 600519, name: '贵州茅台' }]
    const deps = createMockDeps({
      recommendation_tracking: stocks,
      portfolio_positions: [],
    })
    const result = await execSearchStock(deps, 'user1', '贵州')
    expect(result).toContain('600519')
    expect(result).toContain('贵州茅台')
  })
})

describe('execViewPortfolio', () => {
  it('returns empty portfolio message', async () => {
    const deps = createMockDeps({
      portfolios: { free_cash: 50000 },
      portfolio_positions: [],
    })
    const result = await execViewPortfolio(deps, 'user1')
    expect(result).toContain('当前无持仓')
    expect(result).toContain('50,000')
  })

  it('returns formatted positions', async () => {
    const deps = createMockDeps({
      portfolios: { free_cash: 10000 },
      portfolio_positions: [
        { code: '000001', name: '平安银行', shares: 1000, cost_price: 12.5, buy_dt: '2024-01-01', stop_loss: 11.0 },
      ],
    })
    const result = await execViewPortfolio(deps, 'user1')
    expect(result).toContain('持仓 1 只')
    expect(result).toContain('平安银行')
    expect(result).toContain('1000股')
  })
})

describe('execMarketOverview', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ market_signal_daily: [] })
    const result = await execMarketOverview(deps)
    expect(result).toBe('暂无最新市场信号数据')
  })

  it('returns formatted market data', async () => {
    const deps = createMockDeps({
      market_signal_daily: [
        { benchmark_regime: 'RISK_ON', main_index_close: 3200, main_index_today_pct: 1.5, a50_close: 14000, a50_pct_chg: 0.8, vix_close: 15.2 },
      ],
    })
    const result = await execMarketOverview(deps)
    expect(result).toContain('过热禁追')
    expect(result).toContain('禁止新开仓')
    expect(result).toContain('3200')
  })
})

describe('execMarketHistory', () => {
  it('uses TickFlow index K-line history for historical market questions', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: ' tf-test ', tushare_token: '' } })
    deps.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        data: {
          '000001.SH': {
            timestamp: [1704067200000, 1704153600000, 1704240000000],
            open: [3000, 3010, 3020],
            high: [3030, 3040, 3050],
            low: [2990, 3000, 3010],
            close: [3020, 3030, 3040],
            volume: [1000, 1200, 1300],
          },
        },
      }),
    } as Response) as unknown as ToolDeps['fetch']

    const result = await execMarketHistory(deps, 'user1', {}, 100, 'sse')

    expect(result).toBe('mocked LLM response')
    expect(deps.fetch).toHaveBeenCalledWith(
      expect.stringContaining('symbol=000001.SH'),
      expect.objectContaining({ headers: expect.objectContaining({ 'x-api-key': 'tf-test' }) }),
    )
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('最近3个交易日'),
    }))
  })

  it('explains missing TickFlow key', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: '', tushare_token: '' } })

    const result = await execMarketHistory(deps, 'user1', {}, 100, 'sse')

    expect(result).toContain('配置 TickFlow API Key')
    expect(deps.fetch).not.toHaveBeenCalled()
  })
})

describe('execQueryRecommendations', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ recommendation_tracking: [] })
    const result = await execQueryRecommendations(deps, 10)
    expect(result).toBe('暂无形态复盘记录')
  })

  it('formats recommendation entries', async () => {
    const deps = createMockDeps({
      recommendation_tracking: [
        { code: 600519, name: '贵州茅台', recommend_date: 20240101, recommend_count: 3, initial_price: 1800, current_price: 1900, change_pct: 5.56, is_ai_recommended: true },
        { code: 603039, name: '泛微网络', recommend_date: 20240615, recommend_count: 1, initial_price: 46.97, current_price: 44.2, change_pct: -5.9, is_ai_recommended: false },
      ],
      signal_pending: [],
    })
    const result = await execQueryRecommendations(deps, 10)
    expect(result).toContain('600519')
    expect(result).toContain('AI推荐')
    expect(result).toContain('观察/信号复盘')
    expect(result).toContain('入选3次')
    expect(result).toContain('+5.56%')
    expect(result).toContain('-5.90%')
    expect(result).toContain('观察/信号复盘不等于买入')
  })

  it('includes signal_pending entries as pending signals', async () => {
    const deps = createMockDeps({
      recommendation_tracking: [],
      signal_pending: [
        { code: '002079', name: '苏州固锝', signal_date: '2026-06-30', status: 'pending', signal_type: 'lps', signal_score: 0.56, snap_close: 12.3 },
        { code: '600483', name: '福能股份', signal_date: '2026-06-30', status: 'survived', signal_type: 'lps', signal_score: 0.72, snap_close: 10.8 },
        { code: '603661', name: '恒林股份', signal_date: '2026-06-29', status: 'confirmed', signal_type: 'sos', signal_score: 0.9, snap_close: 33.2 },
      ],
    })
    const result = await execQueryRecommendations(deps, 10)
    expect(result).toContain('002079')
    expect(result).toContain('待确认信号')
    expect(result).toContain('600483')
    expect(result).toContain('跨日存活信号')
    expect(result).toContain('603661')
    expect(result).toContain('已确认信号')
    expect(result).toContain('信号日20260630')
  })
})

describe('execQueryAttribution', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ strategy_attribution_reports: [] })
    const result = await execQueryAttribution(deps, 1)
    expect(result).toContain('暂无策略归因报告')
    expect(result).toContain('Web 只读取远端 strategy_attribution_reports')
    expect(result).toContain('query_history(source="attribution")')
  })

  it('formats execution state, latest shadow, and scoped actions', async () => {
    const deps = createMockDeps({
      strategy_attribution_reports: [
        {
          report_date: '2026-07-04',
          window_start: '2026-05-05',
          window_end: '2026-07-04',
          shadow_diff_stats_json: {
            policy_governor: {
              status: 'candidate',
              mode_recommendation: 'review_promote_dynamic_policy',
              next_action: 'manual_review_dynamic_on',
              next_action_summary: 'shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。',
              promotion_status: 'manual_review_required',
              promotion_checklist: [
                { key: 'shadow_sample', status: 'pass', summary: 'sample ok' },
                { key: 'backtest_confirmation', status: 'review', summary: 'need backtest' },
              ],
              auto_apply: false,
              summary: 'shadow 新增组显著优于移除组',
            },
            policy_execution_state: {
              funnel_dynamic_policy: 'shadow',
              horizon: '5',
              scope: 'funnel_shadow',
              active_scope: '漏斗shadow',
              funnel_shadow_weights_active: true,
              funnel_formal_weights_active: false,
              next_action: 'manual_review_dynamic_on',
              next_action_summary: 'shadow 新增组已跑赢移除组；先完成晋级清单和回测复核，再人工决定 dynamic=on。',
              promotion_status: 'manual_review_required',
              promotion_checklist: [
                { key: 'shadow_sample', status: 'pass', summary: 'sample ok' },
                { key: 'backtest_confirmation', status: 'review', summary: 'need backtest' },
              ],
              signal_action_count: 1,
              formal_dynamic_allowed: false,
              formal_dynamic_block_reason: 'manual_review_required',
              summary: 'h=5 调权会影响漏斗 shadow。',
            },
            policy_operations_brief: {
              operator_summary:
                '下一步=shadow 新增组已跑赢移除组；作用范围=funnel_shadow；Shadow=2026-07-03 RISK_ON 新增2 移除1；本期 1 个 scoped 调权：lps[regime=RISK_ON, lane=trend_pullback]×0.50',
            },
            latest: {
              trade_date: '2026-07-03',
              regime: 'RISK_ON',
              selection_summary: {
                base_count: 8,
                shadow_count: 9,
                diff_added_count: 2,
                diff_removed_count: 1,
                jaccard: 0.7,
              },
              diff_added_sample: ['300502', '688008'],
              diff_removed_sample: ['002079'],
            },
          },
          recommendations_json: [
            {
              type: 'downweight',
              horizon: '5',
              target: 'lps',
              reason: {
                weight_multiplier: 0.5,
                scope: { regime: 'RISK_ON', lane: 'trend_pullback' },
                evidence: { avg_return_pct: -3.0, win_rate_pct: 39.8, avg_drawdown_pct: -11.15 },
              },
            },
          ],
        },
      ],
    })

    const result = await execQueryAttribution(deps, 1)

    expect(result).toContain('策略归因报告 2026-07-04')
    expect(result).toContain('数据来源：远端 strategy_attribution_reports')
    expect(result).toContain('晋级=需人工复核')
    expect(result).toContain('晋级检查：样本:通过；回测:待复核')
    expect(result).toContain(
      '执行态：shadow 对照(shadow) | 周期=h5 | 作用范围=漏斗shadow（底层=funnel_shadow） | 晋级=需人工复核 | 下一步=进入人工晋级评审（非正式生效） | 正式dynamic=未进正式漏斗(人工复核未完成) | 可执行调权=1',
    )
    expect(result).toContain('操作摘要：下一步=shadow 新增组已跑赢移除组')
    expect(result).toContain('作用范围=漏斗shadow')
    expect(result).toContain('最新 Shadow：2026-07-03 / RISK_ON | base=8 | shadow=9 | 新增=2 | 移除=1 | Jaccard=0.70')
    expect(result).toContain('Shadow 新增样本：300502, 688008')
    expect(result).toContain('lps[regime=RISK_ON, lane=trend_pullback] | downweight | h=5 | x0.50')
    expect(result).toContain('avg=-3')
  })

  it('synthesizes an operator summary for older reports', async () => {
    const deps = createMockDeps({
      strategy_attribution_reports: [
        {
          report_date: '2026-07-04',
          window_start: '2026-05-05',
          window_end: '2026-07-04',
          shadow_diff_stats_json: {
            policy_governor: {
              next_action: 'manual_review_dynamic_on',
              next_action_summary: 'shadow 新增组已跑赢移除组。',
              promotion_status: 'manual_review_required',
            },
            policy_execution_state: {
              funnel_dynamic_policy: 'shadow',
              horizon: '5',
              scope: 'funnel_shadow',
              next_action: 'manual_review_dynamic_on',
              next_action_summary: 'shadow 新增组已跑赢移除组。',
              promotion_status: 'manual_review_required',
              signal_action_count: 1,
            },
            latest: {
              trade_date: '2026-07-03',
              regime: 'RISK_ON',
              selection_summary: {
                diff_added_count: 2,
                diff_removed_count: 1,
              },
            },
          },
          recommendations_json: [
            {
              type: 'downweight',
              horizon: '5',
              target: 'lps',
              reason: { weight_multiplier: 0.5 },
            },
          ],
        },
      ],
    })

    const result = await execQueryAttribution(deps, 1)

    expect(result).toContain('操作摘要：下一步=shadow 新增组已跑赢移除组。')
    expect(result).toContain('作用范围=漏斗shadow（底层=funnel_shadow）')
    expect(result).toContain('作用范围=漏斗shadow')
    expect(result).toContain('Shadow=2026-07-03 RISK_ON 新增2 移除1')
    expect(result).toContain('调权=1项')
  })

  it('does not let older governor-only reports bypass the promotion checklist', async () => {
    const deps = createMockDeps({
      strategy_attribution_reports: [
        {
          report_date: '2026-07-04',
          window_start: '2026-05-05',
          window_end: '2026-07-04',
          shadow_diff_stats_json: {
            policy_governor: {
              horizon: '5',
              formal_dynamic_allowed: true,
              promotion_status: 'manual_review_required',
              next_action: 'manual_review_dynamic_on',
            },
          },
          recommendations_json: [
            {
              type: 'upweight',
              horizon: '5',
              target: 'sos',
              reason: { weight_multiplier: 1.15 },
            },
          ],
        },
      ],
    })

    const result = await execQueryAttribution(deps, 1)

    expect(result).toContain('作用范围=漏斗shadow（底层=funnel_shadow）')
    expect(result).toContain('正式dynamic=未进正式漏斗(晋级清单缺失)')
    expect(result).not.toContain('正式dynamic=允许正式生效')
  })

  it('does not infer formal activation from governor-only reports even when checklist passed', async () => {
    const deps = createMockDeps({
      strategy_attribution_reports: [
        {
          report_date: '2026-07-04',
          window_start: '2026-05-05',
          window_end: '2026-07-04',
          shadow_diff_stats_json: {
            policy_governor: {
              horizon: '5',
              formal_dynamic_allowed: true,
              promotion_checklist: [
                { key: 'shadow_sample', status: 'pass', summary: 'sample ok' },
                { key: 'backtest_confirmation', status: 'pass', summary: 'backtest ok' },
              ],
              promotion_status: 'manual_review_required',
              next_action: 'manual_review_dynamic_on',
            },
          },
          recommendations_json: [
            {
              type: 'upweight',
              horizon: '5',
              target: 'sos',
              reason: { weight_multiplier: 1.15 },
            },
          ],
        },
      ],
    })

    const result = await execQueryAttribution(deps, 1)

    expect(result).toContain('作用范围=漏斗shadow（底层=funnel_shadow）')
    expect(result).toContain('正式dynamic=未进正式漏斗(缺少后端执行态)')
    expect(result).toContain('缺少后端执行态，默认只按 shadow 展示')
    expect(result).not.toContain('作用范围=正式漏斗（funnel_formal）')
  })
})

describe('execExecutePortfolioUpdate', () => {
  it('handles delete action', async () => {
    const deps = createMockDeps({ portfolio_positions: null })
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'delete', '600519', '贵州茅台', null, null, null)
    expect(result).toContain('已删除')
    expect(result).toContain('600519')
  })

  it('rejects add without required fields', async () => {
    const deps = createMockDeps({})
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', null, null, null, null)
    expect(result).toContain('执行失败')
  })

  it('handles add action with all fields', async () => {
    const deps = createMockDeps({ portfolio_positions: null })
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', '贵州茅台', 100, 1800, 1700, '2026-08-12')
    expect(result).toContain('已新增')
    expect(result).toContain('100股')
  })

  it('updates an existing position without inserting a duplicate row', async () => {
    const { deps, updateChain, insertChain } = createPortfolioWriteDeps([{ id: 'pos-1' }])

    const result = await execExecutePortfolioUpdate(deps, 'user1', 'update', '600519', '贵州茅台', 200, 1810, 1700)

    expect(result).toContain('已更新')
    expect(updateChain.update).toHaveBeenCalledWith(expect.objectContaining({ code: '600519', shares: 200 }))
    expect(updateChain.eq).toHaveBeenCalledWith('portfolio_id', 'USER_LIVE:user1')
    expect(updateChain.eq).toHaveBeenCalledWith('code', '600519')
    expect(insertChain.insert).not.toHaveBeenCalled()
  })

  it('does not reset buy_dt or clear stop_loss when updating without a new stop', async () => {
    const { deps, updateChain } = createPortfolioWriteDeps([{ id: 'pos-1' }])
    const update = updateChain.update as ReturnType<typeof vi.fn>

    await execExecutePortfolioUpdate(deps, 'user1', 'update', '600519', '贵州茅台', 200, 1810, null)

    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ code: '600519', shares: 200, cost_price: 1810 }),
    )
    const payload = update.mock.calls[0]?.[0] as Record<string, unknown>
    expect(payload).not.toHaveProperty('buy_dt')
    expect(payload).not.toHaveProperty('stop_loss')
  })

  it('rejects add without buy_dt so the agent must ask for 建仓日', async () => {
    const deps = createMockDeps({})
    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', '贵州茅台', 100, 1800, 1700)
    expect(result).toContain('缺少建仓日 buy_dt')
  })

  it('writes the provided buy_dt when adding a new position', async () => {
    const insertChain = createMockChain(null)
    const mockFrom = vi.fn().mockReturnValue(insertChain)
    const deps = {
      supabase: { from: mockFrom } as unknown as ToolDeps['supabase'],
      fetch: vi.fn(),
      generateText: vi.fn(),
    } as unknown as ToolDeps

    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', '贵州茅台', 100, 1800, 1700, '2026-08-12')

    expect(result).toContain('已新增')
    expect(insertChain.insert).toHaveBeenCalledWith(
      expect.objectContaining({
        portfolio_id: 'USER_LIVE:user1',
        code: '600519',
        buy_dt: '2026-08-12',
        stop_loss: 1700,
      }),
    )
  })

  it('rejects add with an invalid buy_dt and does not insert', async () => {
    const insertChain = createMockChain(null)
    const deps = {
      supabase: { from: vi.fn().mockReturnValue(insertChain) } as unknown as ToolDeps['supabase'],
      fetch: vi.fn(),
      generateText: vi.fn(),
    } as unknown as ToolDeps

    const result = await execExecutePortfolioUpdate(deps, 'user1', 'add', '600519', '贵州茅台', 100, 1800, 1700, 'yesterday')

    expect(result).toContain('buy_dt 必须是合法日期')
    expect(insertChain.insert).not.toHaveBeenCalled()
  })

  it('does not insert when update matches no rows', async () => {
    const { deps, insertChain } = createPortfolioWriteDeps([])

    const result = await execExecutePortfolioUpdate(deps, 'user1', 'update', '600519', '贵州茅台', 200, 1810, 1700)

    expect(result).toContain('无法 update')
    expect(insertChain.insert).not.toHaveBeenCalled()
  })
})

describe('buildPortfolioWriteRecord', () => {
  it('keeps update payloads free of buy_dt and null stop_loss', () => {
    const record = buildPortfolioWriteRecord('USER_LIVE:u', '600519', 'update', '贵州茅台', 200, 1810, null)
    expect(record).not.toHaveProperty('buy_dt')
    expect(record).not.toHaveProperty('stop_loss')
  })

  it('writes buy_dt and finite stop_loss on add when the date is explicit', () => {
    const record = buildPortfolioWriteRecord('USER_LIVE:u', '600519', 'add', '贵州茅台', 100, 1800, 1700, '2026-08-12')
    expect(record.buy_dt).toBe('2026-08-12')
    expect(record.stop_loss).toBe(1700)
  })
})

describe('execScreenStocks', () => {
  it('returns no-data message when empty', async () => {
    const deps = createMockDeps({ recommendation_tracking: [] })
    const result = await execScreenStocks(deps)
    expect(result.stocks).toEqual([])
    expect(result.meta.ai_count).toBe(0)
  })

  it('keeps optional strategy policy evidence in the output schema', () => {
    const result = SCREEN_RESULT_OUTPUT_SCHEMA.parse({
      date: '2026-07-05',
      stocks: [],
      meta: { ai_count: 0 },
      strategy_policy: {
        dynamic_mode: 'shadow',
        policy_weight_active_scope: '漏斗shadow',
        selection_action_count: 1,
        selection_action_summary: '候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75',
        attribution_signal_weights: { lps: 0.5 },
      },
    })

    expect(result.strategy_policy?.selection_action_count).toBe(1)
    expect(result.strategy_policy?.selection_action_summary).toContain('candidate_lane=trend_pullback')
  })

  it('attaches latest strategy policy evidence from shadow attribution runs', async () => {
    const deps = createMockDeps({
      recommendation_tracking: [
        {
          code: 300502,
          name: '新易盛',
          recommend_date: '2026-07-05',
          funnel_score: 0.9,
          change_pct: 3.2,
          candidate_lane: 'trend_pullback',
          entry_type: 'mainline',
          is_ai_recommended: true,
        },
      ],
      signal_policy_shadow_runs: [
        {
          trade_date: '2026-07-04',
          shadow_diff_stats_json: {
            policy_governor: { horizon: '5', next_action: 'review_policy_actions' },
            policy_execution_state: { funnel_dynamic_policy: 'shadow' },
            policy_operations_brief: {
              active_scope: '漏斗shadow',
              selection_action_count: 1,
              selection_action_summary: '候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75',
              formal_dynamic_allowed: false,
            },
          },
          recommendations_json: [
            {
              type: 'selection_downweight',
              horizon: '5',
              target: 'trend_pullback',
              reason: '{"weight_multiplier":0.75}',
            },
          ],
        },
      ],
    })

    const result = await execScreenStocks(deps)

    expect(result.strategy_policy?.policy_weight_active_scope).toBe('漏斗shadow')
    expect(result.strategy_policy?.selection_action_summary).toContain('candidate_lane=trend_pullback')
    expect(result.strategy_policy?.attribution_signal_weights).toEqual({ trend_pullback: 0.75 })
  })

  it('keeps optional strategy policy evidence in strategy decision output schema', () => {
    const result = STRATEGY_DECISION_OUTPUT_SCHEMA.parse({
      summary: '组合保持轻仓',
      market_regime: 'RISK_ON',
      overall_position: '30%',
      risk: '主线分歧日不追高',
      position_actions: [],
      strategy_policy: {
        dynamic_mode: 'shadow',
        selection_action_summary: '候选源治理 1 项：candidate_lane=lps 降级',
        attribution_signal_weights: { lps: 0.5 },
      },
    })

    expect(result.strategy_policy?.dynamic_mode).toBe('shadow')
    expect(result.strategy_policy?.attribution_signal_weights).toEqual({ lps: 0.5 })
  })
})

describe('execStrategyDecision', () => {
  it('returns latest strategy policy evidence when portfolio is empty', async () => {
    const deps = createMockDeps({
      portfolio_positions: [],
      market_signal_daily: { benchmark_regime: 'RISK_ON' },
      signal_policy_shadow_runs: [
        {
          shadow_diff_stats_json: {
            policy_governor: { horizon: '5', next_action: 'review_policy_actions' },
            policy_execution_state: { funnel_dynamic_policy: 'shadow' },
            policy_operations_brief: {
              active_scope: '漏斗shadow',
              selection_action_count: 1,
              selection_action_summary: '候选源治理 1 项：candidate_lane=lps 降级到 shadow/人工复核×0.50',
            },
          },
          recommendations_json: [
            {
              type: 'selection_downweight',
              horizon: '5',
              target: 'lps',
              reason: '{"weight_multiplier":0.5}',
            },
          ],
        },
      ],
    })

    const result = await execStrategyDecision(deps, 'user1', {})

    expect(result.overall_position).toBe('空仓')
    expect(result.strategy_policy?.policy_weight_active_scope).toBe('漏斗shadow')
    expect(result.strategy_policy?.attribution_signal_weights).toEqual({ lps: 0.5 })
    expect(deps.generateText).not.toHaveBeenCalled()
  })

  it('adds strategy policy evidence to the decision prompt and output', async () => {
    const deps = createMockDeps({
      portfolio_positions: [{ code: '600519', name: '贵州茅台', shares: 100, cost_price: 1800, stop_loss: 1700 }],
      market_signal_daily: { benchmark_regime: 'RISK_ON', main_index_close: 4000 },
      signal_policy_shadow_runs: [
        {
          shadow_diff_stats_json: {
            policy_governor: { horizon: '5', next_action: 'review_policy_actions' },
            policy_execution_state: { funnel_dynamic_policy: 'shadow' },
            policy_operations_brief: {
              active_scope: '漏斗shadow',
              selection_action_count: 1,
              selection_action_summary: '候选源治理 1 项：candidate_lane=trend_pullback 降级',
            },
          },
          recommendations_json: [
            {
              type: 'selection_downweight',
              horizon: '5',
              target: 'trend_pullback',
              reason: '{"weight_multiplier":0.75}',
            },
          ],
        },
      ],
    })
    deps.generateText = vi.fn().mockResolvedValue({
      output: {
        summary: '持有为主',
        market_regime: 'RISK_ON',
        overall_position: '30%',
        risk: '控制回撤',
        position_actions: [],
      },
    }) as unknown as ToolDeps['generateText']

    const result = await execStrategyDecision(deps, 'user1', {})

    expect(result.strategy_policy?.attribution_signal_weights).toEqual({ trend_pullback: 0.75 })
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('候选源治理 1 项：candidate_lane=trend_pullback 降级'),
    }))
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('下一步: 先复核调权治理项'),
    }))
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('归因调权: trend_pullback×0.75'),
    }))
  })
})

describe('execGenerateAiReport', () => {
  it('adds strategy policy context to the report and prompt', async () => {
    const deps = createMockDeps({
      user_settings: { tickflow_api_key: ' tf-test ', tushare_token: '' },
      signal_policy_shadow_runs: [
        {
          shadow_diff_stats_json: {
            policy_governor: { horizon: '5', next_action: 'review_policy_actions' },
            policy_execution_state: { funnel_dynamic_policy: 'shadow' },
            policy_operations_brief: {
              active_scope: '漏斗shadow',
              selection_action_summary: '候选源治理 1 项：candidate_lane=sos 降级',
            },
          },
          recommendations_json: [
            {
              type: 'selection_downweight',
              horizon: '5',
              target: 'sos',
              reason: '{"weight_multiplier":0.8}',
            },
          ],
        },
      ],
    })
    deps.fetch = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          data: [
            { date: '2026-07-01', open: 10, high: 11, low: 9.8, close: 10.8, volume: 1000 },
            { date: '2026-07-02', open: 10.8, high: 11.2, low: 10.6, close: 11, volume: 1200 },
          ],
        }),
      })
      .mockResolvedValue({ ok: false, json: () => Promise.resolve({}) }) as unknown as ToolDeps['fetch']

    const result = await execGenerateAiReport(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      ['600519'],
    )

    expect(result).toContain('### 策略治理')
    expect(result).toContain('下一步: 先复核调权治理项')
    expect(result).toContain('候选源治理 1 项：candidate_lane=sos 降级')
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('归因调权: sos×0.80'),
    }))
  })
})

describe('execAnalyzeStock', () => {
  it('includes value snapshot when analyzing A-share stocks', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: ' tf-test ', tushare_token: '' } })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          data: [
            { date: '2024-01-01', open: 100, high: 103, low: 99, close: 102, volume: 1000 },
            { date: '2024-01-02', open: 102, high: 105, low: 101, close: 104, volume: 1200 },
          ],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          data: {
            '600519.SH': [{
              period_end: '2026-03-31',
              roe: 18.2,
              net_income_yoy: 11.8,
              revenue_yoy: 6.5,
              gross_margin: 91.6,
              net_margin: 48.3,
              debt_to_asset_ratio: 21.4,
              operating_cash_to_revenue: 16.2,
            }],
          },
        }),
      })
    deps.fetch = fetchMock as unknown as ToolDeps['fetch']

    const result = await execAnalyzeStock(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      '600519',
      '贵州茅台',
    )

    expect(result.markdown).toBe('mocked LLM response')
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/llm-proxy/v1/financials/metrics?'),
      expect.objectContaining({ headers: expect.objectContaining({ 'x-api-key': 'tf-test' }) }),
    )
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      system: expect.stringContaining('价值面校准'),
      prompt: expect.stringContaining('价值面摘要（来源：TickFlow，报告期：2026-03-31）'),
    }))
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringContaining('K线共2根'),
    }))
  })

  it('uses TickFlow batch fallback for market symbols', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: ' tf-test ', tushare_token: '' } })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: {} }) })
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          data: {
            'AAPL.US': {
              timestamp: [1704067200000, 1704153600000],
              open: [100, 101],
              high: [102, 103],
              low: [99, 100],
              close: [101, 102],
              volume: [1000, 1200],
            },
          },
        }),
      })
    deps.fetch = fetchMock as unknown as ToolDeps['fetch']

    const result = await execAnalyzeStock(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      'AAPL.US',
      '苹果',
    )

    expect(result.markdown).toBe('mocked LLM response')
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('/api/llm-proxy/v1/klines/batch?'),
      expect.objectContaining({ headers: expect.objectContaining({ 'x-api-key': 'tf-test' }) }),
    )
  })

  it('normalizes TickFlow epoch-second timestamps for analyze_stock digests', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: 'tf-test', tushare_token: '' } })
    deps.fetch = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({
          data: {
            '600519.SH': {
              timestamp: [1704067200, 1704153600],
              open: [100, 101],
              high: [102, 103],
              low: [99, 100],
              close: [101, 102],
              volume: [1000, 1200],
            },
          },
        }),
      })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: {} }) }) as unknown as ToolDeps['fetch']

    await execAnalyzeStock(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      '600519',
      '贵州茅台',
    )

    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.stringMatching(/2024-01-01[\s\S]*2024-01-02/),
    }))
    expect(deps.generateText).toHaveBeenCalledWith(expect.objectContaining({
      prompt: expect.not.stringContaining('1970-'),
    }))
  })

  it('explains missing TickFlow key for market symbols', async () => {
    const deps = createMockDeps({ user_settings: { tickflow_api_key: '', tushare_token: '' } })

    const result = await execAnalyzeStock(
      deps,
      'user1',
      { api_key: 'llm-key', model: 'test-model', base_url: 'https://example.com/v1' },
      {},
      'AAPL.US',
      '苹果',
    )

    expect(result.summary).toContain('设置页配置 TickFlow API Key')
    expect(deps.fetch).not.toHaveBeenCalled()
  })
})
