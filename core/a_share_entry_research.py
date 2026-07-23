"""Research-only A-share entry policies derived from realized signal outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.candidate_policy import candidate_score_value


@dataclass(frozen=True)
class AShareEntryResearchPolicy:
    blocked_confirmed_signals: tuple[str, ...] = ()
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
CONFIRMED_SIGNAL_PRIOR = {
    "trend_pullback": 1.00,
    "trend_lane_pullback": 1.00,
    "trend_breakout": 0.95,
    "main_force_entry": 0.95,
    "mainline": 0.95,
    "lps": 0.90,
    "compression": 0.75,
    "spring": 0.65,
    "sos": 0.35,
    "evr": 0.10,
}


def normalized_signal_type(raw: object) -> str:
    return str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def confirmed_signal_allowed(policy: AShareEntryResearchPolicy, signal_type: object) -> bool:
    blocked = {normalized_signal_type(item) for item in policy.blocked_confirmed_signals}
    return normalized_signal_type(signal_type) not in blocked


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
    data = breadth or {}
    return (
        _number(data.get("ratio_pct")) >= policy.neutral_breadth_ratio_min
        and _number(data.get("delta_pct")) >= policy.neutral_breadth_delta_min
        and _number(data.get("daily_up_ratio_pct")) >= policy.neutral_daily_up_ratio_min
        and int(data.get("sample_size") or 0) >= policy.neutral_breadth_sample_min
    )


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
