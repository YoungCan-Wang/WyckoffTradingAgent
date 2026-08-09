"""Research-only A-share entry policies derived from realized signal outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.candidate_policy import candidate_score_value


@dataclass(frozen=True)
class AShareEntryResearchPolicy:
    blocked_confirmed_signals: tuple[str, ...] = ()
    blocked_confirmed_regime_signals: tuple[tuple[str, str], ...] = ()
    entry_weight_multipliers: tuple[tuple[str, str, float], ...] = ()
    max_hold_days_by_regime_signal: tuple[tuple[str, str, int], ...] = ()
    require_neutral_breadth_confirmation: bool = False
    calibrate_confirmed_score: bool = False
    neutral_breadth_ratio_min: float = 50.0
    neutral_breadth_delta_min: float = 0.0
    neutral_daily_up_ratio_min: float = 50.0
    neutral_breadth_sample_min: int = 100


# Research priors only. Production promotion still requires cross-period and
# walk-forward evidence; a Wyckoff label alone never grants execution priority.
# 确认信号的类型先验。数值越高表示该形态确认后越值得给分。
#
# 2026-08-09 用 run 31293167694（五个市场环境、3594 笔成交）校准了一次：
#   trend_pullback  prior 1.00   实测 +0.86%  n=171
#   lps             prior 0.90   实测 -2.95%  n=11   （样本不足，不动）
#   compression     prior 0.75   实测 +1.44%  n=30   （样本不足，不动）
#   spring          prior 0.65   实测 -3.15%  n=1285 ← 唯一明确错配
#   sos             prior 0.35   实测 -0.99%  n=876
#   evr             prior 0.10   实测 -2.66%  n=1221
# prior 与实测收益的 Spearman = +0.257，方向大致正确，只有 spring 定得过高。
#
# spring 0.65 → 0.15：它是唯一"样本充足且五个周期方向全部为负"的类型
# （-4.66 / -4.45 / -4.68 / -1.62 / -2.68，周期均值标准差仅 1.39），
# 对照其余类型 Welch t=-4.26。0.15 使其落到 evr(0.10) 与 sos(0.35) 之间，
# 与实测末位相称，且与实盘 _trigger_score 的 spring 降权（45→12）方向一致。
#
# 未调 trend_pullback / compression / lps：trend_pullback 虽实测最好但逐周期
# 方向不稳（+3.12 / +0.98 / -9.25 / -5.93 / +12.26，标准差 8.39），
# compression(30 笔) 与 lps(11 笔) 样本不足，按它们调整等于拟合噪声。
CONFIRMED_SIGNAL_PRIOR = {
    "trend_pullback": 1.00,
    "trend_lane_pullback": 1.00,
    "trend_breakout": 0.95,
    "main_force_entry": 0.95,
    "mainline": 0.95,
    "lps": 0.90,
    "compression": 0.75,
    "sos": 0.35,
    "spring": 0.15,
    "evr": 0.10,
}


def normalized_signal_type(raw: object) -> str:
    return str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def confirmed_signal_allowed(
    policy: AShareEntryResearchPolicy,
    signal_type: object,
    regime: object = "",
) -> bool:
    signal = normalized_signal_type(signal_type)
    regime_key = str(regime or "").strip().upper()
    blocked = {normalized_signal_type(item) for item in policy.blocked_confirmed_signals}
    blocked_pairs = {
        (str(item_regime).strip().upper(), normalized_signal_type(item_signal))
        for item_regime, item_signal in policy.blocked_confirmed_regime_signals
    }
    if signal in blocked or (regime_key, signal) in blocked_pairs:
        return False
    return True


def entry_weight_multiplier(
    policy: AShareEntryResearchPolicy,
    signal_type: object,
    regime: object,
) -> float:
    signal = normalized_signal_type(signal_type)
    regime_key = str(regime or "").strip().upper()
    for configured_regime, configured_signal, multiplier in policy.entry_weight_multipliers:
        if regime_key == str(configured_regime).strip().upper() and signal == normalized_signal_type(configured_signal):
            return min(max(float(multiplier), 0.0), 1.0)
    return 1.0


def research_max_hold_days(
    policy: AShareEntryResearchPolicy,
    signal_type: object,
    regime: object,
    default_days: int,
) -> int:
    default = max(int(default_days), 1)
    signal = normalized_signal_type(signal_type)
    regime_key = str(regime or "").strip().upper()
    for configured_regime, configured_signal, hold_days in policy.max_hold_days_by_regime_signal:
        if regime_key == str(configured_regime).strip().upper() and signal == normalized_signal_type(configured_signal):
            return min(max(int(hold_days), 1), default)
    return default


def market_context_allows_entry(
    policy: AShareEntryResearchPolicy,
    *,
    regime: object,
    breadth: dict[str, Any] | None,
) -> bool:
    if not policy.require_neutral_breadth_confirmation:
        return True
    if str(regime or "").strip().upper() != "NEUTRAL":
        return True
    return _neutral_breadth_confirmed(policy, breadth)


def calibrated_confirmation_score(policy: AShareEntryResearchPolicy, signal_type: object, raw_score: object) -> float:
    score = candidate_score_value(raw_score)
    if not policy.calibrate_confirmed_score:
        return score
    signal = normalized_signal_type(signal_type)
    prior = CONFIRMED_SIGNAL_PRIOR.get(signal, 0.50)
    bounded_strength = min(max(score, 0.0) / 20.0, 1.0)
    return 20.0 * (0.65 * prior + 0.35 * bounded_strength)


def _number(raw: object) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float("-inf")
    return value if value == value else float("-inf")


def _neutral_breadth_confirmed(policy: AShareEntryResearchPolicy, breadth: dict[str, Any] | None) -> bool:
    data = breadth or {}
    return (
        _number(data.get("ratio_pct")) >= policy.neutral_breadth_ratio_min
        and _number(data.get("delta_pct")) >= policy.neutral_breadth_delta_min
        and _number(data.get("daily_up_ratio_pct")) >= policy.neutral_daily_up_ratio_min
        and int(data.get("sample_size") or 0) >= policy.neutral_breadth_sample_min
    )
