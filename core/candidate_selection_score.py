"""Shadow scoring for candidate selection review.

The score is intentionally observational: it is persisted in
signal_observations.features_json and should not change live candidate
selection until outcome data proves that it helps.
"""

from __future__ import annotations

from typing import Any

from core.dynamic_shadow_score import DynamicShadowConfig, build_dynamic_shadow_score
from utils.safe import parse_cn_num

CANDIDATE_SHADOW_SCORE_VERSION = "candidate_shadow_score_v3"

_ACCUM_SIGNALS = {"spring", "lps", "compression"}


def _num(raw: Any, default: float = 0.0) -> float:
    value = parse_cn_num(raw)
    return default if value is None else value


def _bounded100(raw: Any) -> float:
    value = _num(raw)
    return max(0.0, min(100.0, value))


def _candidate_score100(raw: Any) -> float:
    value = _num(raw)
    if value <= 0:
        return 0.0
    if value <= 1.0:
        value *= 100.0
    elif value <= 20.0:
        value *= 5.0
    return max(0.0, min(100.0, value))


def _points(score100: Any, max_points: float) -> float:
    return round(_bounded100(score100) / 100.0 * max_points, 1)


def _grade(score: float) -> str:
    if score >= 85:
        return "S"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _funnel_component(trigger_score: float, priority_score: float) -> tuple[float, list[str]]:
    raw = priority_score if priority_score > 0 else trigger_score
    normalized = _candidate_score100(raw)
    component = _points(normalized, 30.0)
    tags = []
    if normalized >= 80:
        tags.append("high_funnel_priority")
    elif normalized >= 60:
        tags.append("medium_funnel_priority")
    return component, tags


def _price_action_component(signal_type: str, footprint: dict[str, Any]) -> tuple[float, float, list[str], list[str]]:
    if not footprint:
        return 0.0, 0.0, [], []
    absorption = _bounded100(footprint.get("absorption_score"))
    dry_up = _bounded100(footprint.get("dry_up_score"))
    breakout = _bounded100(footprint.get("breakout_quality_score"))
    reclaim = _bounded100(footprint.get("reclaim_score"))
    supply = _bounded100(footprint.get("supply_pressure_score"))
    failed = _bounded100(footprint.get("failed_breakout_score"))

    if str(signal_type or "").strip().lower() in _ACCUM_SIGNALS:
        quality = absorption * 0.33 + dry_up * 0.27 + reclaim * 0.25 + breakout * 0.15
    else:
        quality = breakout * 0.35 + absorption * 0.25 + dry_up * 0.20 + reclaim * 0.20
    component = _points(quality, 30.0)

    positive = [str(tag) for tag in footprint.get("tags") or [] if str(tag).strip()]
    if str(footprint.get("bias") or "").strip().lower() == "demand":
        positive.append("demand_bias")
    negative = [str(tag) for tag in footprint.get("negative_tags") or [] if str(tag).strip()]
    if supply >= 70:
        negative.append("supply_pressure")
    if failed >= 60:
        negative.append("failed_breakout")
    penalty = min(20.0, _points(supply, 12.0) + _points(failed, 8.0) + (4.0 if "weak_close" in negative else 0.0))
    return component, round(-penalty, 1), _unique(positive), _unique(negative)


def _springboard_component(springboard: dict[str, Any]) -> tuple[float, list[str]]:
    if not springboard:
        return 0.0, []
    met_count = max(0.0, min(3.0, _num(springboard.get("springboard_met_count"))))
    bool_hits = sum(1 for key in ("springboard_a", "springboard_b", "springboard_c") if bool(springboard.get(key)))
    quality = max(met_count, float(bool_hits)) / 3.0 * 100.0
    component = _points(quality, 18.0)
    tags = []
    grade = str(springboard.get("springboard_grade") or "").strip()
    if grade and grade.lower() != "none":
        tags.append(f"springboard:{grade}")
    if max(met_count, float(bool_hits)) >= 2:
        tags.append("springboard_structure_ready")
    return component, tags


def _lhb_capital_score(source_context: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    score, positive, negative = 0.0, [], []
    lhb = source_context.get("lhb") or {}
    lhb_net = parse_cn_num(lhb.get("net_buy")) if isinstance(lhb, dict) else None
    if lhb_net is not None and lhb_net > 0:
        score += 3.0
        positive.append("lhb_net_buy")
    elif lhb_net is not None and lhb_net < 0:
        negative.append("lhb_net_sell")
    if isinstance(lhb, dict):
        institution_net = parse_cn_num(lhb.get("institution_net_buy"))
        connect_net = parse_cn_num(lhb.get("connect_seat_net_buy"))
        if institution_net is not None and institution_net > 0:
            score += 1.5
            positive.append("institution_net_buy")
        elif institution_net is not None and institution_net < 0:
            negative.append("institution_net_sell")
        if connect_net is not None and connect_net > 0:
            score += 1.0
            positive.append("connect_seat_net_buy")
        elif connect_net is not None and connect_net < 0:
            negative.append("connect_seat_net_sell")
    return score, positive, negative


def _margin_block_score(source_context: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    score, positive, negative = 0.0, [], []

    margin = source_context.get("margin") or {}
    if isinstance(margin, dict):
        margin_buy = parse_cn_num(margin.get("margin_buy"))
        margin_repay = parse_cn_num(margin.get("margin_repay")) or 0.0
        if margin_buy is not None and margin_buy > 0 and margin_buy > margin_repay:
            score += 1.5
            positive.append("margin_buying")
        short_sell = parse_cn_num(margin.get("short_sell"))
        short_repay = parse_cn_num(margin.get("short_repay"))
        if short_sell is not None and short_repay is not None and short_sell > short_repay > 0:
            negative.append("short_selling_pressure")

    block_trade = source_context.get("block_trade") or {}
    total_amount = parse_cn_num(block_trade.get("total_amount")) if isinstance(block_trade, dict) else None
    if total_amount is not None and total_amount > 0:
        score += 1.0
        if "avg_discount_pct" in block_trade:
            discount = parse_cn_num(block_trade.get("avg_discount_pct"))
            if discount is not None and discount >= 0:
                score += 0.5
                positive.append("block_trade_premium")
            elif discount is not None and discount <= -3:
                negative.append("block_trade_discount")
    return score, positive, negative


def _flow_capital_score(source_context: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    score, positive, negative = 0.0, [], []

    tick = source_context.get("tick_large_order") or {}
    tick_net = parse_cn_num(tick.get("large_net_amount_yuan")) if isinstance(tick, dict) else None
    if tick_net is not None and tick_net > 0:
        score += 2.0
        positive.append("large_order_net_buy")
    elif tick_net is not None and tick_net < 0:
        negative.append("large_order_net_sell")

    moneyflow = source_context.get("stock_moneyflow") or {}
    moneyflow_net = parse_cn_num(moneyflow.get("net_amount_wan")) if isinstance(moneyflow, dict) else None
    if moneyflow_net is not None and moneyflow_net > 0:
        score += 1.5
        positive.append("stock_moneyflow_net_buy")
    elif moneyflow_net is not None and moneyflow_net < 0:
        negative.append("stock_moneyflow_net_sell")

    connect = source_context.get("hsgt_top10") or {}
    connect_net = parse_cn_num(connect.get("net_amount")) if isinstance(connect, dict) else None
    if connect_net is not None and connect_net > 0:
        score += 1.0
        positive.append("hsgt_top10_net_buy")
    elif connect_net is not None and connect_net < 0:
        negative.append("hsgt_top10_net_sell")

    return score, positive, negative


def _external_capital_component(source_context: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    if not source_context:
        return 0.0, [], []
    parts = (
        _lhb_capital_score(source_context),
        _margin_block_score(source_context),
        _flow_capital_score(source_context),
    )
    score = sum(part[0] for part in parts)
    positive = [tag for part in parts for tag in part[1]]
    negative = [tag for part in parts for tag in part[2]]

    return round(min(score, 8.0), 1), positive, negative


def score_candidate_shadow(
    *,
    signal_type: str,
    trigger_score: float,
    priority_score: float = 0.0,
    footprint: dict[str, Any] | None = None,
    springboard: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    health_context: dict[str, Any] | None = None,
    dynamic_config: DynamicShadowConfig | None = None,
) -> dict[str, Any]:
    """Build a compact, deterministic candidate-quality score."""

    positive: list[str] = []
    negative: list[str] = []
    funnel_score, funnel_tags = _funnel_component(trigger_score, priority_score)
    price_action, risk_penalty, pa_tags, pa_negative = _price_action_component(signal_type, footprint or {})
    springboard_score, spring_tags = _springboard_component(springboard or {})
    external_score, external_tags, external_negative = _external_capital_component(source_context or {})

    positive.extend(funnel_tags + pa_tags + spring_tags + external_tags)
    negative.extend(pa_negative + external_negative)
    components = {
        "funnel": funnel_score,
        "price_action": price_action,
        "springboard": springboard_score,
        "external_capital": external_score,
        "risk_penalty": risk_penalty,
    }
    total = round(max(0.0, min(100.0, sum(components.values()))), 1)
    grade = _grade(total)
    dynamic = build_dynamic_shadow_score(
        base_score=total,
        base_grade=grade,
        health=health_context,
        springboard=springboard,
        source_context=source_context,
        negative_tags=_unique(negative),
        config=dynamic_config,
    )
    return {
        "version": CANDIDATE_SHADOW_SCORE_VERSION,
        "score": total,
        "grade": grade,
        "components": components,
        "positive_tags": _unique(positive),
        "negative_tags": _unique(negative),
        "score_inputs": {
            "trigger_score": round(_num(trigger_score), 4),
            "priority_score": round(_num(priority_score), 4),
        },
        "dynamic": dynamic,
    }
