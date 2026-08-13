"""Outcome-calibrated shadow score and Step3 promotion eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.safe import parse_cn_num

DYNAMIC_SHADOW_SCORE_VERSION = "dynamic_shadow_score_v1"
_STOCK_SPECIFIC_CAPITAL = {
    "lhb",
    "margin",
    "block_trade",
    "stock_moneyflow",
    "hsgt_top10",
    "tick_large_order",
}
_HARD_RISK_TAGS = {
    "failed_breakout",
    "supply_pressure",
    "weak_close",
    "lhb_net_sell",
    "large_order_net_sell",
    "stock_moneyflow_net_sell",
    "institution_net_sell",
}


@dataclass(frozen=True)
class DynamicShadowConfig:
    min_base_score: float = 65.0
    min_dynamic_score: float = 75.0
    min_health_samples: int = 30
    min_health_weight: float = 0.9
    min_springboard_conditions: int = 2
    require_stock_capital: bool = False


def dynamic_shadow_config_from_env() -> DynamicShadowConfig:
    from utils.env import env_bool, env_float, env_int

    return DynamicShadowConfig(
        min_base_score=env_float("FUNNEL_DYNAMIC_SHADOW_MIN_BASE_SCORE", 65.0),
        min_dynamic_score=env_float("FUNNEL_DYNAMIC_SHADOW_MIN_SCORE", 75.0),
        min_health_samples=env_int("FUNNEL_DYNAMIC_SHADOW_MIN_SAMPLES", 30, minimum=1),
        min_health_weight=env_float("FUNNEL_DYNAMIC_SHADOW_MIN_HEALTH_WEIGHT", 0.9),
        min_springboard_conditions=env_int("FUNNEL_DYNAMIC_SHADOW_MIN_SPRINGBOARD", 2, minimum=0),
        require_stock_capital=env_bool("FUNNEL_DYNAMIC_SHADOW_REQUIRE_STOCK_CAPITAL", False),
    )


def _number(raw: Any, default: float = 0.0) -> float:
    value = parse_cn_num(raw)
    return default if value is None else float(value)


def _stock_capital_providers(source_context: dict[str, Any]) -> list[str]:
    return sorted(
        key
        for key in _STOCK_SPECIFIC_CAPITAL
        if isinstance(source_context.get(key), dict) and bool(source_context.get(key))
    )


def _health_adjustment(health: dict[str, Any]) -> float:
    if not health:
        return 0.0
    weight = max(0.0, min(_number(health.get("weight_multiplier"), 0.75), 1.2))
    return round(max(-8.0, min(8.0, (weight - 0.75) * 20.0)), 1)


def _promotion_checks(
    *,
    base_score: float,
    dynamic_score: float,
    health: dict[str, Any],
    springboard: dict[str, Any],
    source_context: dict[str, Any],
    negative_tags: list[str],
    config: DynamicShadowConfig,
) -> dict[str, bool]:
    providers = _stock_capital_providers(source_context)
    state = str(health.get("health_state") or "INSUFFICIENT").strip().upper()
    return {
        "base_score": base_score >= config.min_base_score,
        "dynamic_score": dynamic_score >= config.min_dynamic_score,
        "signal_health": state == "HEALTHY"
        and int(_number(health.get("sample_count"))) >= config.min_health_samples
        and _number(health.get("weight_multiplier"), 0.0) >= config.min_health_weight,
        "springboard_structure": int(_number(springboard.get("springboard_met_count")))
        >= config.min_springboard_conditions,
        "stock_specific_capital": bool(providers) or not config.require_stock_capital,
        "risk_clear": not bool(_HARD_RISK_TAGS.intersection(negative_tags)),
    }


def build_dynamic_shadow_score(
    *,
    base_score: float,
    base_grade: str,
    health: dict[str, Any] | None,
    springboard: dict[str, Any] | None,
    source_context: dict[str, Any] | None,
    negative_tags: list[str] | None,
    config: DynamicShadowConfig | None = None,
) -> dict[str, Any]:
    """Calibrate one candidate and expose auditable promotion gates."""
    cfg = config or DynamicShadowConfig()
    health_ctx = dict(health or {})
    springboard_ctx = dict(springboard or {})
    capital_ctx = dict(source_context or {})
    risks = list(negative_tags or [])
    adjustment = _health_adjustment(health_ctx)
    dynamic_score = round(max(0.0, min(100.0, float(base_score) + adjustment)), 1)
    checks = _promotion_checks(
        base_score=float(base_score),
        dynamic_score=dynamic_score,
        health=health_ctx,
        springboard=springboard_ctx,
        source_context=capital_ctx,
        negative_tags=risks,
        config=cfg,
    )
    eligible = all(checks.values())
    blockers = [key for key, passed in checks.items() if not passed]
    return {
        "version": DYNAMIC_SHADOW_SCORE_VERSION,
        "base_score": round(float(base_score), 1),
        "base_grade": str(base_grade or ""),
        "score": dynamic_score,
        "health_adjustment": adjustment,
        "health": {
            "state": str(health_ctx.get("health_state") or "INSUFFICIENT").strip().upper(),
            "sample_count": int(_number(health_ctx.get("sample_count"))),
            "weight_multiplier": round(_number(health_ctx.get("weight_multiplier"), 0.75), 3),
            "regime": str(health_ctx.get("regime") or "ALL").strip().upper(),
            "horizon_days": int(_number(health_ctx.get("horizon_days"))),
        },
        "stock_capital_providers": _stock_capital_providers(capital_ctx),
        "promotion": {
            "status": "eligible" if eligible else "watch",
            "eligible": eligible,
            "checks": checks,
            "blockers": blockers,
        },
    }
