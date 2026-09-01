/**
 * 前端展示用的执行闸门判定 —— 必须与 Python 执行侧同口径。
 *
 * 为什么单独成文件：`chat-tools.ts` 里原先内联了一份自算闸门，与真正下单的
 * Python 侧三处不一致，于是 chat 报的「执行闸门」和系统实际行为对不上：
 *
 * 1. 盘前 `UNKNOWN` / `RISK_OFF` 被当成硬禁买。这两条路径 Python 侧已按实测
 *    裁掉（见 `core/market_trade_mode.py` 的 `merge_premarket_regime`）：3 个
 *    盘前 UNKNOWN 日全部是外部取数失败而非「看不清」，08-17 一天就白扔了 71 只
 *    formal_l4 / 168 只候选；3 个 RISK_OFF 日全由单条「VIX 涨幅 >= 8%」触发，
 *    而 VIX 取的是隔夜美股收盘，天然滞后一天。
 * 2. 收盘态漏了 `NEUTRAL`。生产 `STEP4_BUY_BLOCK_REGIMES` 含 NEUTRAL，实际禁买，
 *    前端却报「允许进入 OMS 复核」。
 * 3. 收盘态多了 `BEAR_REBOUND`。生产 `STEP4_BUY_ALLOW_REGIMES=BEAR_REBOUND`
 *    显式豁免，实际放行，前端却报禁止新开仓。
 *
 * `integrations/supabase_market_signal.py` 已经因为同款自算翻过两次车（生产实测
 * 06-18 / 08-07 / 08-17 三天横幅与 trade mode 直接相反），修法都是「不自算，改调
 * merge_premarket_regime + resolve_market_trade_mode」。前端读不到 env、也读不到
 * 那两个函数（`action_phrase` 那组结构化列在写失败时会被整组剥掉，不能当作稳定
 * 数据源），只能重算一份；那就把它收敂到这里，用测试锁住与 Python 的一致性。
 *
 * 改这里之前先改 Python 侧：本文件是镜像，不是真源。
 */

/**
 * 盘前态唯一还能收紧执行权限的档位。
 * 镜像 `core/market_trade_mode.py` 的 `PREMARKET_ESCALATION_REGIMES`。
 */
export const PREMARKET_ESCALATION_REGIMES: ReadonlySet<string> = new Set(['BLACK_SWAN'])

/**
 * 合法收盘态。镜像 `core/market_trade_mode.py` 的 `KNOWN_MARKET_REGIMES`。
 *
 * 注意不含 `NORMAL`：那是盘前态专用标签（「隔夜外部冲击相对平稳」），
 * 收盘态只由 `tools/market_regime.py` 产出这 10 个值。收盘态拿到表外的值
 * （含 NORMAL）按 `normalize_regime` 一律归成 UNKNOWN，即禁买。
 */
export const KNOWN_MARKET_REGIMES: ReadonlySet<string> = new Set([
  'RISK_ON',
  'NEUTRAL',
  'CAUTION',
  'BEAR_REBOUND',
  'PANIC_REPAIR',
  'PANIC_REPAIR_CONFIRMED',
  'PANIC_REPAIR_INTRADAY',
  'RISK_OFF',
  'CRASH',
  'BLACK_SWAN',
])

/** 镜像 `core/market_trade_mode.py` 的 `normalize_regime`。 */
export function normalizeRegime(regime: string | null | undefined): string {
  const normalized = String(regime || '').trim().toUpperCase()
  return KNOWN_MARKET_REGIMES.has(normalized) ? normalized : 'UNKNOWN'
}

/**
 * 收盘态禁止新开仓的水温。
 *
 * 镜像 `core/market_trade_mode.py` 的 `oms_buy_block_regimes()` 在生产 env 下的取值：
 * `EXECUTE_BLOCK_NEW_BUY_REGIMES`（UNKNOWN/RISK_OFF/CRASH/BLACK_SWAN/RISK_ON/
 * BEAR_REBOUND/PANIC_REPAIR）并上 `STEP4_BUY_BLOCK_REGIMES`（额外加 NEUTRAL）,
 * 再减去 `STEP4_BUY_ALLOW_REGIMES`（BEAR_REBOUND）。
 *
 * env 由 `.github/workflows/wyckoff_funnel.yml` 与 `step4_from_supabase.yml` 提供。
 * 运维改动那两个变量时，这里要跟着改——`web/packages/shared/src/market-regime-gate.test.ts`
 * 会把当前口径钉住，不同步就会红。
 */
export const BENCHMARK_BUY_BLOCK_REGIMES: ReadonlySet<string> = new Set([
  'UNKNOWN',
  'NEUTRAL',
  'RISK_ON',
  'PANIC_REPAIR',
  'RISK_OFF',
  'CRASH',
  'BLACK_SWAN',
])

/**
 * 只允许二次确认后小额 PROBE 的水温。
 * 镜像 `core/market_trade_mode.py` 的 `PROBE_ONLY_REGIMES`。
 */
export const PROBE_ONLY_REGIMES: ReadonlySet<string> = new Set([
  'CAUTION',
  'PANIC_REPAIR_CONFIRMED',
  'PANIC_REPAIR_INTRADAY',
])

/**
 * 合并收盘态与盘前态，得出实际生效的水温。
 * 镜像 `core/market_trade_mode.py` 的 `merge_premarket_regime`：盘前态只有落在
 * `PREMARKET_ESCALATION_REGIMES` 时才生效，其余（RISK_OFF / UNKNOWN / CAUTION /
 * DATA_GAP / 缺失 / 拼错）一律回落到收盘态。
 */
export function mergePremarketRegime(benchmark: string | null | undefined, premarket: string | null | undefined): string {
  const benchmarkNorm = normalizeRegime(benchmark)
  const premarketNorm = String(premarket || '').trim().toUpperCase()
  if (!PREMARKET_ESCALATION_REGIMES.has(premarketNorm)) return benchmarkNorm
  // BLACK_SWAN 是 MARKET_EXECUTION_PRIORITY 里最严的一档（优先级 0），收紧即取它。
  return 'BLACK_SWAN'
}

export type ExecutionGateLevel = 'blocked' | 'probe_only' | 'allowed'

export interface ExecutionGate {
  /** 合并后实际生效的水温。 */
  effectiveRegime: string
  level: ExecutionGateLevel
  /** 给用户看的一行说明。 */
  text: string
}

/** 按收盘态 + 盘前态算出执行闸门。 */
export function resolveExecutionGate(
  benchmark: string | null | undefined,
  premarket: string | null | undefined,
): ExecutionGate {
  const effectiveRegime = mergePremarketRegime(benchmark, premarket)
  if (BENCHMARK_BUY_BLOCK_REGIMES.has(effectiveRegime)) {
    return { effectiveRegime, level: 'blocked', text: '执行闸门：禁止新开仓，只管理已有仓位' }
  }
  if (PROBE_ONLY_REGIMES.has(effectiveRegime)) {
    return { effectiveRegime, level: 'probe_only', text: '执行闸门：仅允许二次确认后的 PROBE，禁止 ATTACK' }
  }
  return { effectiveRegime, level: 'allowed', text: '执行闸门：允许 confirmed 候选进入 OMS 复核' }
}
