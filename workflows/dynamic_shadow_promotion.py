"""Build dynamic-shadow scores and promote qualified rows to Step3 review."""

from __future__ import annotations

import os
from typing import Any

from core.candidate_metadata import code6
from core.candidate_selection_score import score_candidate_shadow
from core.dynamic_shadow_score import dynamic_shadow_config_from_env
from core.signal_feedback import normalize_signal_type
from workflows.daily_signal_observations import build_external_capital_context_map, load_dynamic_shadow_health_map


def _enabled() -> bool:
    """晋级默认关闭：影子分照算照记，但不占 Step3 席位。

    晋级判据是「信号 health 为 HEALTHY」，而生产数据不支持这个前提。用事件日之前的
    health 做前瞻检验（12,635 行 health × 27,240 行 outcome）：HEALTHY 的前瞻 T+5 为
    −12.74%，是四档里最差的（WATCH −4.51%、DECAYED −5.48%、INSUFFICIENT −2.87%）；
    同一交易日内配对后差值仍为 −2.82pct、配对 bootstrap 95% CI [−5.22, −0.26]；
    health 的历史统计与实际前瞻收益相关系数 −0.116，呈均值回复而非延续。

    docs/ITERATION_STRATEGY.md 的接线纪律也要求 shadow 停在「score_only」，
    「lift 稳定为正才考虑升成排序键」——该条件尚未满足。样本仍薄（HEALTHY 仅 133 个
    事件、其中 launchpad 占 101 个，剔除后差值落回噪声），所以结论是「无正向证据」
    而非「已证否」；等样本跑够并复算转正后再开。
    """
    return os.getenv("FUNNEL_DYNAMIC_SHADOW_PROMOTION", "0").strip().lower() not in {"0", "false", "no", "off"}


def _health_for_signal(health_map: dict[Any, dict], signal: str, regime: str) -> dict:
    regime_key = str(regime or "NEUTRAL").strip().upper()
    return dict(health_map.get((signal, regime_key)) or health_map.get((signal, "ALL")) or health_map.get(signal) or {})


def _candidate_items(step2_details: dict) -> list[tuple[str, str, float]]:
    triggers = step2_details.get("review_triggers") or step2_details.get("triggers") or {}
    items: list[tuple[str, str, float]] = []
    for signal_type, hits in triggers.items():
        signal = normalize_signal_type(signal_type)
        items.extend((signal, code6(code), float(score or 0.0)) for code, score in hits or [] if code6(code))
    return items


def _best_rows(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        code = row["code"]
        current = best.get(code)
        if current is None or float(row["dynamic_score"]) > float(current["dynamic_score"]):
            best[code] = row
    return sorted(best.values(), key=lambda row: (-float(row["dynamic_score"]), row["code"]))


def _score_rows(step2_details: dict, regime: str) -> list[dict]:
    springboard_map = step2_details.get("springboard_map") or {}
    footprint_map = step2_details.get("footprint_map") or {}
    source_context_map = step2_details.get("source_context_map") or {}
    health_map = step2_details.get("dynamic_shadow_health_map") or {}
    priority_map = step2_details.get("priority_score_map") or {}
    config = dynamic_shadow_config_from_env()
    rows = []
    for signal, code, trigger_score in _candidate_items(step2_details):
        scored = score_candidate_shadow(
            signal_type=signal,
            trigger_score=trigger_score,
            priority_score=float(priority_map.get(code) or 0.0),
            footprint=footprint_map.get(f"{signal}:{code}") or footprint_map.get(code),
            springboard=springboard_map.get(f"{signal}:{code}") or springboard_map.get(code),
            source_context=source_context_map.get(f"{signal}:{code}") or source_context_map.get(code),
            health_context=_health_for_signal(health_map, signal, regime),
            dynamic_config=config,
        )
        dynamic = scored["dynamic"]
        rows.append(
            {
                "code": code,
                "signal_type": signal,
                "base_score": scored["score"],
                "dynamic_score": dynamic["score"],
                "promotion": dynamic["promotion"],
                "health": dynamic["health"],
                "capital_providers": dynamic["stock_capital_providers"],
            }
        )
    return _best_rows(rows)


def _score_candidates(step2_details: dict) -> list[str]:
    ranked: dict[str, float] = {}
    for signal, code, score in _candidate_items(step2_details):
        del signal
        ranked[code] = max(ranked.get(code, float("-inf")), score)
    return sorted(ranked, key=lambda code: (-ranked[code], code))


def _external_context_candidates(step2_details: dict) -> list[str]:
    """资金上下文的取数范围。与 FUNNEL_EXTERNAL_CAPITAL_MAX_SYMBOLS 同因放宽到 400。"""
    limit = max(int(float(os.getenv("FUNNEL_DYNAMIC_SHADOW_CONTEXT_CANDIDATES", "400"))), 1)
    return _score_candidates(step2_details)[:limit]


def prepare_dynamic_shadow_promotions(
    step2_details: dict,
    regime: str,
    *,
    trade_date: str,
    logs_path: str | None = None,
    log_fn=None,
) -> list[dict]:
    if not _enabled() or not step2_details:
        return []
    from core.price_action_footprint import build_price_action_footprint_map
    from workflows.daily_signal_observations import build_springboard_map

    step2_details.setdefault("springboard_map", build_springboard_map(step2_details))
    step2_details.setdefault(
        "footprint_map",
        build_price_action_footprint_map(
            step2_details.get("review_triggers") or step2_details.get("triggers") or {},
            step2_details.get("all_df_map") or {},
        ),
    )
    step2_details.setdefault("dynamic_shadow_health_map", load_dynamic_shadow_health_map(regime))
    if not step2_details["dynamic_shadow_health_map"]:
        step2_details["dynamic_shadow_scores"] = _score_rows(step2_details, regime)
        step2_details["dynamic_shadow_promoted"] = []
        return []
    if "source_context_map" not in step2_details:
        step2_details["source_context_map"] = build_external_capital_context_map(
            step2_details,
            _external_context_candidates(step2_details),
            logs_path,
            trade_date=trade_date,
            log_fn=log_fn,
        )
    rows = _score_rows(step2_details, regime)
    eligible = [row for row in rows if row["promotion"].get("eligible")]
    cap = max(int(float(os.getenv("FUNNEL_DYNAMIC_SHADOW_PROMOTION_CAP", "1"))), 0)
    promoted = eligible[:cap]
    step2_details["dynamic_shadow_scores"] = rows
    step2_details["dynamic_shadow_promoted"] = promoted
    return promoted
