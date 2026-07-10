import { describe, expect, it } from 'vitest'
import { formatPolicyWeightMetaText, formatStrategyPolicyText } from '@wyckoff/shared'

describe('formatPolicyWeightMetaText', () => {
  it('formats snapshot policy metadata with active scope', () => {
    expect(formatPolicyWeightMetaText({
      source: '远端',
      report_date: '2026-07-04',
      horizon: '5',
      execution_policy: 'on',
      execution_scope: 'tail_buy_and_funnel',
      tail_buy_weights_active: true,
      funnel_shadow_weights_active: true,
      funnel_formal_weights_active: true,
    })).toBe('（远端, 报告=2026-07-04, 周期=h5, 策略=正式调权(on), 范围=尾盘+正式漏斗）')
  })

  it('formats persisted tail-buy features with prefixed keys', () => {
    expect(formatPolicyWeightMetaText({
      policy_weight_source: '远端',
      policy_weight_report_date: '2026-07-04',
      policy_weight_horizon: '5',
      policy_weight_age_days: 0,
      policy_weight_execution_policy: 'shadow',
      policy_weight_execution_scope: 'tail_buy_and_funnel_shadow',
      policy_weight_next_action: 'manual_review_dynamic_on',
      policy_weight_formal_dynamic_allowed: false,
      policy_weight_formal_dynamic_block_reason: 'shadow_only',
      policy_weight_tail_buy_weights_active: true,
      policy_weight_funnel_shadow_weights_active: true,
      policy_weight_funnel_formal_weights_active: false,
    })).toBe(
      '（远端, 报告=2026-07-04, 周期=h5, 距今=0天, 策略=shadow 对照(shadow), 下一步=进入人工晋级评审（非正式生效）, 范围=尾盘+漏斗shadow, 正式dynamic=未进正式漏斗(仅 shadow 观察)）',
    )
  })

  it('labels missing promotion checklist without leaking raw gate codes', () => {
    expect(formatPolicyWeightMetaText({
      source: '远端',
      formal_dynamic_allowed: false,
      formal_dynamic_block_reason: 'promotion_checklist=missing',
    })).toBe('（远端, 正式dynamic=未进正式漏斗(晋级清单缺失)）')
  })

  it('labels blocked promotion checklist with evidence', () => {
    expect(formatPolicyWeightMetaText({
      source: '远端',
      formal_dynamic_allowed: false,
      formal_dynamic_block_reason: 'promotion_checklist=shadow_sample:review',
    })).toBe('（远端, 正式dynamic=未进正式漏斗(晋级清单未通过(样本:待复核))）')
  })

  it('labels selection action review blocker without leaking raw gate codes', () => {
    expect(formatPolicyWeightMetaText({
      source: '远端',
      formal_dynamic_allowed: false,
      formal_dynamic_block_reason: 'selection_actions_review_required',
    })).toBe('（远端, 正式dynamic=未进正式漏斗(候选源治理待复核)）')
  })

  it('labels policy action review next step from persisted feature keys', () => {
    expect(formatPolicyWeightMetaText({
      policy_weight_source: '远端',
      policy_weight_next_action: 'review_policy_actions',
    })).toBe('（远端, 下一步=先复核调权治理项）')
  })

  it('prefers persisted display labels over raw policy codes', () => {
    expect(formatPolicyWeightMetaText({
      policy_weight_source: '远端',
      policy_weight_execution_policy: 'shadow',
      policy_weight_execution_policy_label: 'shadow 对照(shadow)',
      policy_weight_next_action: 'manual_review_dynamic_on',
      policy_weight_next_action_label: '进入人工晋级评审（非正式生效）',
      policy_weight_formal_dynamic_allowed: false,
      policy_weight_formal_dynamic_block_reason: 'shadow_only',
      policy_weight_formal_dynamic_label: '未进正式漏斗(仅 shadow 观察)',
    })).toBe(
      '（远端, 策略=shadow 对照(shadow), 下一步=进入人工晋级评审（非正式生效）, 正式dynamic=未进正式漏斗(仅 shadow 观察)）',
    )
  })

  it('labels missing backend execution state as a formal blocker', () => {
    expect(formatPolicyWeightMetaText({
      source: '远端',
      formal_dynamic_allowed: false,
      formal_dynamic_block_reason: 'execution_state=missing',
    })).toBe('（远端, 正式dynamic=未进正式漏斗(缺少后端执行态)）')
  })

  it('derives active scope from legacy execution scope without echoing raw scope', () => {
    expect(formatPolicyWeightMetaText({
      source: '远端',
      execution_policy: 'shadow',
      execution_scope: 'tail_buy_and_funnel_shadow',
    })).toBe('（远端, 策略=shadow 对照(shadow), 范围=尾盘+漏斗shadow）')
  })
})

describe('formatStrategyPolicyText', () => {
  it('preserves token order and explicit label precedence', () => {
    expect(formatStrategyPolicyText({
      selection_action_summary: '候选源治理=降权 spring',
      attribution_signal_weights: { spring: 0.8 },
      policy_weight_active_scope: '尾盘+正式漏斗',
      execution_policy: 'off',
      execution_policy_label: '正式调权(on)',
      dynamic_mode_label: '不应使用',
      next_action: 'observe_only',
      next_action_label: '人工复核后生效',
    })).toBe('候选源治理=降权 spring / 归因调权 spring×0.80 / 正式调权(on) / 尾盘+正式漏斗 / 下一步=人工复核后生效')
  })

  it('suppresses the empty summary and default observe action', () => {
    expect(formatStrategyPolicyText({
      selection_action_summary: '候选源治理=无',
      dynamic_mode: 'shadow',
    })).toBe('shadow 对照(shadow)')
  })

  it('formats at most six finite attribution weights', () => {
    expect(formatStrategyPolicyText({
      attribution_signal_weights: {
        spring: 1,
        lps: 0.9,
        sos: 0.8,
        test: Number.NaN,
        sc: 0.7,
        ar: 0.6,
        st: 0.5,
        ut: 0.4,
      },
    })).toBe('归因调权 spring×1.00，lps×0.90，sos×0.80，sc×0.70，ar×0.60，st×0.50 / 未知模式')
  })

  it('returns empty text when policy data is absent', () => {
    expect(formatStrategyPolicyText(null)).toBe('')
  })
})
