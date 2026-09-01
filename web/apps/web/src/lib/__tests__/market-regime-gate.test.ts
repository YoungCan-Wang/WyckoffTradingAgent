import { describe, expect, it } from 'vitest'
import {
  BENCHMARK_BUY_BLOCK_REGIMES,
  mergePremarketRegime,
  normalizeRegime,
  PREMARKET_ESCALATION_REGIMES,
  resolveExecutionGate,
} from '@wyckoff/shared'

// 下面这张表是从 Python 侧实跑出来的，命令：
//   STEP4_BUY_BLOCK_REGIMES="UNKNOWN,NEUTRAL,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN" \
//   STEP4_BUY_ALLOW_REGIMES="BEAR_REBOUND" \
//   .venv/bin/python -c "from core.market_trade_mode import merge_premarket_regime as m; ..."
// 改 Python 侧后要重跑并同步这张表。
describe('mergePremarketRegime 与 Python 逐例一致', () => {
  const cases: Array<[string | null, string | null, string]> = [
    // 盘前 BLACK_SWAN 是唯一还能收紧的档位。
    ['NEUTRAL', 'BLACK_SWAN', 'BLACK_SWAN'],
    ['CAUTION', 'BLACK_SWAN', 'BLACK_SWAN'],
    // 盘前 RISK_OFF 不再降级：只由单条「VIX 涨幅 >= 8%」触发，而 VIX 是隔夜美股收盘，滞后一天。
    ['NEUTRAL', 'RISK_OFF', 'NEUTRAL'],
    // 盘前 UNKNOWN 不再降级：实测 3 天全是外部取数失败，不是「看不清」。
    ['NEUTRAL', 'UNKNOWN', 'NEUTRAL'],
    // DATA_GAP / 缺失 / 拼错 / CAUTION 一律回落到收盘态。
    ['NEUTRAL', 'DATA_GAP', 'NEUTRAL'],
    ['NEUTRAL', '', 'NEUTRAL'],
    ['NEUTRAL', null, 'NEUTRAL'],
    ['NEUTRAL', 'risk_of', 'NEUTRAL'],
    ['NEUTRAL', 'CAUTION', 'NEUTRAL'],
    // 大小写与空白不影响判定。
    [' neutral ', ' black_swan ', 'BLACK_SWAN'],
    // 收盘态缺失或不在 KNOWN_MARKET_REGIMES 里，一律归 UNKNOWN。
    [null, 'NORMAL', 'UNKNOWN'],
    ['NORMAL', 'NORMAL', 'UNKNOWN'],
    ['typo', 'NORMAL', 'UNKNOWN'],
  ]

  for (const [benchmark, premarket, expected] of cases) {
    it(`${JSON.stringify(benchmark)} + ${JSON.stringify(premarket)} -> ${expected}`, () => {
      expect(mergePremarketRegime(benchmark, premarket)).toBe(expected)
    })
  }

  it('盘前只有 BLACK_SWAN 一档能收紧', () => {
    expect([...PREMARKET_ESCALATION_REGIMES]).toEqual(['BLACK_SWAN'])
  })
})

describe('normalizeRegime', () => {
  it('NORMAL 不是合法收盘态 —— 它只是盘前标签', () => {
    expect(normalizeRegime('NORMAL')).toBe('UNKNOWN')
    expect(resolveExecutionGate('NORMAL', 'NORMAL').level).toBe('blocked')
  })

  it('10 个合法收盘态原样返回', () => {
    for (const regime of [
      'RISK_ON', 'NEUTRAL', 'CAUTION', 'BEAR_REBOUND', 'PANIC_REPAIR',
      'PANIC_REPAIR_CONFIRMED', 'PANIC_REPAIR_INTRADAY', 'RISK_OFF', 'CRASH', 'BLACK_SWAN',
    ]) {
      expect(normalizeRegime(regime)).toBe(regime)
    }
  })
})

describe('BENCHMARK_BUY_BLOCK_REGIMES 与生产 env 同口径', () => {
  // 实跑 oms_buy_block_regimes() 得到:
  // ['BLACK_SWAN','CRASH','NEUTRAL','PANIC_REPAIR','RISK_OFF','RISK_ON','UNKNOWN']
  it('名单与 Python oms_buy_block_regimes() 逐项一致', () => {
    expect([...BENCHMARK_BUY_BLOCK_REGIMES].sort()).toEqual([
      'BLACK_SWAN', 'CRASH', 'NEUTRAL', 'PANIC_REPAIR', 'RISK_OFF', 'RISK_ON', 'UNKNOWN',
    ])
  })

  it('NEUTRAL 禁买 —— 生产 STEP4_BUY_BLOCK_REGIMES 明确加了它', () => {
    expect(resolveExecutionGate('NEUTRAL', 'NORMAL').level).toBe('blocked')
  })

  it('BEAR_REBOUND 被 STEP4_BUY_ALLOW_REGIMES 豁免,不禁买', () => {
    expect(BENCHMARK_BUY_BLOCK_REGIMES.has('BEAR_REBOUND')).toBe(false)
    expect(resolveExecutionGate('BEAR_REBOUND', 'NORMAL').level).toBe('allowed')
  })
})

describe('resolveExecutionGate', () => {
  it('CAUTION 只放二次确认后的 PROBE', () => {
    const gate = resolveExecutionGate('CAUTION', 'NORMAL')
    expect(gate.level).toBe('probe_only')
    expect(gate.text).toContain('PROBE')
  })

  it('盘前 CAUTION 不再单独触发 PROBE 档 —— 收盘态说了算', () => {
    expect(resolveExecutionGate('BEAR_REBOUND', 'CAUTION').level).toBe('allowed')
  })

  it('禁买优先于 PROBE —— 盘前 BLACK_SWAN 压过收盘 CAUTION', () => {
    const gate = resolveExecutionGate('CAUTION', 'BLACK_SWAN')
    expect(gate.effectiveRegime).toBe('BLACK_SWAN')
    expect(gate.level).toBe('blocked')
  })

  it('PANIC_REPAIR_CONFIRMED 走 PROBE,不是禁买', () => {
    expect(resolveExecutionGate('PANIC_REPAIR_CONFIRMED', 'NORMAL').level).toBe('probe_only')
  })
})
