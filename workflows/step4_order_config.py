"""Runtime configuration loader for Step4 OMS order rules."""

from __future__ import annotations

import os

from core.market_trade_mode import EXECUTE_BLOCK_NEW_BUY_REGIMES
from utils.env import env_bool as _env_bool
from utils.env import env_float as _env_float
from workflows.step4_models import Step4OrderConfig


def step4_order_config_from_env() -> Step4OrderConfig:
    gap_min = max(_env_float("STEP4_CHASE_GAP_PCT_MIN", 1.2), 0.2)
    atr_min = max(_env_float("STEP4_CHASE_ATR_MULT_MIN", 0.8), 0.1)
    return Step4OrderConfig(
        atr_multiplier=_env_float("STEP4_ATR_MULTIPLIER", 2.0),
        buy_hard_stop_enabled=_env_bool("STEP4_BUY_HARD_STOP_ENABLED", True),
        buy_hard_stop_pct=max(_env_float("STEP4_BUY_HARD_STOP_PCT", 12.0), 0.0),
        buy_stop_mode=_env_stop_mode("STEP4_BUY_STOP_MODE", "floor"),
        atr_slippage_factor=_env_float("STEP4_ATR_SLIPPAGE_FACTOR", 0.25),
        probe_budget_limit=_clamp01(_env_float("STEP4_PROBE_BUDGET_LIMIT", 0.10)),
        repair_probe_budget_limit=_clamp01(_env_float("STEP4_REPAIR_PROBE_BUDGET_LIMIT", 0.05)),
        attack_budget_limit=_clamp01(_env_float("STEP4_ATTACK_BUDGET_LIMIT", 0.20)),
        buy_block_regimes=_env_regime_set(
            "STEP4_BUY_BLOCK_REGIMES",
            "RISK_ON,BEAR_REBOUND,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN",
        ),
        block_buy_on_stale_exit=_env_bool("STEP4_BLOCK_BUY_ON_STALE_EXIT", True),
        chase_gap_pct_min=gap_min,
        chase_gap_pct_max=max(_env_float("STEP4_CHASE_GAP_PCT_MAX", 5.5), gap_min),
        chase_atr_mult_min=atr_min,
        chase_atr_mult_max=max(_env_float("STEP4_CHASE_ATR_MULT_MAX", 2.4), atr_min),
        max_gap_up_pct=_env_float("STEP4_MAX_GAP_UP_PCT", 3.0),
        max_gap_up_atr_mult=_env_float("STEP4_MAX_GAP_UP_ATR_MULT", 1.5),
    )


def _env_stop_mode(name: str, default: str) -> str:
    mode = os.getenv(name, default).strip().lower()
    return mode if mode in {"fixed", "floor"} else default


def _env_regime_set(name: str, default: str) -> frozenset[str]:
    values = {
        item.strip().upper()
        for item in os.getenv(name, default).split(",")
        if item.strip() and item.strip().upper() != "COOLDOWN"
    }
    return frozenset(values | set(EXECUTE_BLOCK_NEW_BUY_REGIMES))


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)
