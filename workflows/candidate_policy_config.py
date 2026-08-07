"""Runtime configuration loader for candidate selection policy."""

from __future__ import annotations

import os

from core.candidate_policy import CandidatePolicyConfig
from utils.env import env_bool as _env_bool
from utils.env import env_float as _env_float
from utils.env import env_int as _env_int


def candidate_policy_config_from_env() -> CandidatePolicyConfig:
    evr_min_score = _env_optional_float("FUNNEL_LOSS_GUARD_PURE_EVR_MIN_SCORE")
    return CandidatePolicyConfig(
        loss_guard_enabled=_env_bool("FUNNEL_LOSS_GUARD_ENABLED", True),
        alpha_block_risk_on_early_breakout=_env_bool("FUNNEL_ALPHA_BLOCK_RISK_ON_EARLY_BREAKOUT", True),
        alpha_risk_on_early_breakout_min_score=_env_float("FUNNEL_ALPHA_RISK_ON_EARLY_BREAKOUT_MIN_SCORE", 70.0),
        mix_trendpb_min_score=_env_float("FUNNEL_LOSS_GUARD_MIX_TRENDPB_MIN_SCORE", 12.0),
        pure_lps_observe_only=_env_bool("FUNNEL_LOSS_GUARD_PURE_LPS_OBSERVE_ONLY", True),
        pure_lps_min_score=_env_float("FUNNEL_LOSS_GUARD_PURE_LPS_MIN_SCORE", 6.0),
        pure_trendpb_observe_only=_env_bool("FUNNEL_LOSS_GUARD_PURE_TRENDPB_OBSERVE_ONLY", True),
        pure_trendpb_min_score=_env_float("FUNNEL_LOSS_GUARD_PURE_TRENDPB_MIN_SCORE", 14.0),
        pure_sos_min_score=_env_float("FUNNEL_LOSS_GUARD_PURE_SOS_MIN_SCORE", 6.0),
        pure_sos_observe_only=_env_bool("FUNNEL_LOSS_GUARD_PURE_SOS_OBSERVE_ONLY", True),
        pure_evr_observe_only=_env_bool("FUNNEL_LOSS_GUARD_PURE_EVR_OBSERVE_ONLY", True),
        pure_evr_min_score_default=evr_min_score if evr_min_score is not None else 3.0,
        pure_evr_min_score_hot=evr_min_score if evr_min_score is not None else 5.0,
        weak_confirmation_min_abc=_env_int("FUNNEL_LOSS_GUARD_WEAK_CONFIRMATION_MIN_ABC", 2, minimum=0),
        pure_sos_min_abc=_env_int("FUNNEL_LOSS_GUARD_PURE_SOS_MIN_ABC", 3, minimum=0),
        risk_on_pre5_ret=_env_float("FUNNEL_LOSS_GUARD_RISK_ON_PRE5_RET", 35.0),
        risk_on_range_pos=_env_float("FUNNEL_LOSS_GUARD_RISK_ON_RANGE_POS", 85.0),
        risk_on_vol_ratio=_env_float("FUNNEL_LOSS_GUARD_RISK_ON_VOL_RATIO", 1.8),
        defensive_high_range_pos=_env_float("FUNNEL_LOSS_GUARD_DEFENSIVE_HIGH_RANGE_POS", 78.0),
        defensive_high_20d_ret=_env_float("FUNNEL_LOSS_GUARD_DEFENSIVE_HIGH_20D_RET", 18.0),
        neutral_high_range_pos=_env_float("FUNNEL_LOSS_GUARD_NEUTRAL_HIGH_RANGE_POS", 90.0),
        neutral_high_20d_ret=_env_float("FUNNEL_LOSS_GUARD_NEUTRAL_HIGH_20D_RET", 35.0),
        max_structure_stop_pct=_env_float("FUNNEL_MAX_STRUCTURE_STOP_PCT", 12.0),
    )


def _env_optional_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
