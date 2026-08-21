"""Structured payloads produced by the funnel run."""

from __future__ import annotations

from typing import Any

from core.candidate_metadata import build_candidate_metadata_map, code6
from core.candidate_policy import candidate_score_value
from core.candidate_tracks import candidate_entry_track
from core.funnel_report import build_symbol_report_row, candidate_reason_text
from core.funnel_taxonomy import source_label
from workflows.funnel_ai_selection import FunnelAiSelection
from workflows.funnel_settings import FUNNEL_L2_BYPASS_AI_CAP, FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP
from workflows.step4_order_config import resolve_live_market_trade_mode


def context_regime(ctx: Any) -> str:
    return str(getattr(ctx, "regime", "NEUTRAL") or "NEUTRAL")


def legacy_symbol_rows(ctx: Any, selection: FunnelAiSelection) -> list[dict]:
    metadata_map = _candidate_metadata_map(ctx)
    sos_hit_set = {str(c).strip() for c, _ in ctx.review_triggers.get("sos", [])}
    evr_hit_set = {str(c).strip() for c, _ in ctx.review_triggers.get("evr", [])}
    spring_hit_set = {str(c).strip() for c, _ in ctx.review_triggers.get("spring", [])}
    lps_hit_set = {str(c).strip() for c, _ in ctx.review_triggers.get("lps", [])}

    def infer_track(code: str) -> str:
        if code in ctx.candidate_entry_map:
            return candidate_entry_track(ctx.candidate_entry_map[code])
        if code in sos_hit_set or code in evr_hit_set:
            return "Trend"
        return "Accum" if code in spring_hit_set or code in lps_hit_set else "Trend"

    return [
        _with_run_readiness(
            _with_candidate_metadata(
                build_symbol_report_row(
                    code,
                    rank=idx + 1,
                    tag=candidate_reason_text(code, ctx.code_to_reasons, ctx.theme_badge_map),
                    track=infer_track(code),
                    stage=stage_name(ctx, code),
                    score=legacy_display_score(ctx, code),
                    priority_score=float(ctx.code_to_total_score.get(code, 0.0)),
                    selection_source=legacy_selection_source(ctx, code),
                    selection_is_fill=False,
                    market_regime=context_regime(ctx),
                    maps=ctx.report_maps,
                ),
                code,
                metadata_map,
            ),
            ctx,
        )
        for idx, code in enumerate(selection.selected_for_ai)
    ]


def modern_symbol_rows(ctx: Any, selection: FunnelAiSelection) -> list[dict]:
    metadata_map = _candidate_metadata_map(ctx)
    return [
        _with_run_readiness(
            _with_candidate_metadata(
                build_symbol_report_row(
                    code,
                    rank=idx + 1,
                    tag=f"{_source_tag(ctx, code)} | {candidate_reason_text(code, ctx.code_to_reasons, ctx.theme_badge_map)}",
                    track=selected_track(selection, code),
                    stage=stage_name(ctx, code),
                    score=display_score(ctx, selection, code),
                    priority_score=display_score(ctx, selection, code),
                    selection_source=selection_source(ctx, code),
                    selection_is_fill=selection_source(ctx, code) == "l3_fill",
                    market_regime=context_regime(ctx),
                    maps=ctx.report_maps,
                ),
                code,
                metadata_map,
            ),
            ctx,
        )
        for idx, code in enumerate(selection.selected_for_ai)
    ]


def _candidate_metadata_map(ctx: Any) -> dict[str, dict[str, Any]]:
    return build_candidate_metadata_map(
        getattr(ctx, "candidate_entries", []) or [],
        getattr(ctx, "mainline_candidates", []) or [],
    )


def _with_candidate_metadata(row: dict[str, Any], code: str, metadata_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row.update(metadata_map.get(code6(code), {}))
    return row


def _with_run_readiness(row: dict[str, Any], ctx: Any) -> dict[str, Any]:
    quality = (getattr(ctx, "metrics", {}) or {}).get("data_quality") or {}
    row["trade_readiness"] = str(quality.get("trade_readiness") or "ready")
    row["data_quality_status"] = str(quality.get("status") or "normal")
    return row


def _source_tag(ctx: Any, code: str) -> str:
    if code in getattr(ctx, "mainline_candidate_set", set()):
        return "主线买点确认"
    if code in ctx.strategic_l2_bypass_set:
        return source_label("strategic_l2_bypass")
    if code in ctx.l2_bypass_set:
        return source_label("l2_bypass")
    return str(ctx.l2_channel_map.get(code, "")).strip() or "正式候选"


def funnel_run_details(
    ctx: Any,
    selection: FunnelAiSelection,
    *,
    content: str,
    title: str,
    symbols: list[dict],
) -> dict:
    trade_mode = resolve_live_market_trade_mode(context_regime(ctx))
    trade_mode_payload = _trade_mode_payload(ctx, trade_mode)
    return {
        "metrics": ctx.metrics,
        "trade_mode": trade_mode_payload,
        "strategy_policy": _strategy_policy_payload(selection.ai_policy),
        "triggers": ctx.review_triggers,
        "review_triggers": ctx.review_triggers,
        "formal_triggers": ctx.formal_triggers,
        "confirmation_triggers": ctx.formal_triggers,
        "l2_bypass_triggers": ctx.bypass_triggers,
        "pattern_bypass_triggers": ctx.bypass_triggers,
        "l2_bypass_selected": [c for c in selection.selected_for_ai if c in ctx.l2_bypass_set],
        "pattern_bypass_selected": [c for c in selection.selected_for_ai if c in ctx.l2_bypass_set],
        "l2_bypass_budget": FUNNEL_L2_BYPASS_AI_CAP,
        "strategic_l2_bypass_triggers": ctx.strategic_l2_bypass_triggers,
        "strategic_theme_triggers": ctx.strategic_l2_bypass_triggers,
        "strategic_l2_bypass_selected": [c for c in selection.selected_for_ai if c in ctx.strategic_l2_bypass_set],
        "strategic_theme_selected": [c for c in selection.selected_for_ai if c in ctx.strategic_l2_bypass_set],
        "strategic_l2_bypass_budget": FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP,
        "mainline_candidates": getattr(ctx, "mainline_candidates", []),
        "mainline_selected": [
            c for c in selection.selected_for_ai if c in getattr(ctx, "mainline_candidate_set", set())
        ],
        "leader_radar_rows": ctx.leader_radar_rows,
        "leader_radar_symbols": sorted(ctx.leader_radar_symbols),
        "candidate_entries": ctx.candidate_entries,
        "external_seed_triggers": ctx.external_seed_triggers,
        "external_seed_selected": [],
        "content": content,
        "title": title,
        "symbols_for_report": symbols,
        "selected_for_ai": selection.selected_for_ai,
        "trend_selected": selection.trend_selected,
        "accum_selected": selection.accum_selected,
        "priority_score_map": display_score_map(ctx, selection),
        "shadow_added": selection.ai_policy.get("shadow_added", []) or [],
        "shadow_removed": selection.ai_policy.get("shadow_removed", []) or [],
        "shadow_score_map": selection.ai_policy.get("shadow_score_map", {}) or {},
        "name_map": ctx.name_map,
        "sector_map": ctx.sector_map,
        "all_df_map": ctx.all_df_map,
    }


def _trade_mode_payload(ctx: Any, trade_mode: Any) -> dict[str, Any]:
    payload = {
        "regime": trade_mode.regime,
        "mode": trade_mode.mode,
        "label": trade_mode.label,
        "action": trade_mode.action,
        "reason": trade_mode.reason,
        "allow_ai_review": trade_mode.allow_ai_review,
        "allow_recommendation_write": trade_mode.allow_recommendation_write,
    }
    quality = (getattr(ctx, "metrics", {}) or {}).get("data_quality") or {}
    if quality.get("trade_readiness") == "observe_only":
        payload.update(
            mode="observe_only",
            label="数据质量降级观察",
            action="仅保留 AI/shadow 观察，禁止正式推荐和新开仓",
            reason=", ".join(quality.get("reasons") or []) or "data_quality_degraded",
            allow_ai_review=True,
            allow_recommendation_write=False,
        )
    return payload


def _strategy_policy_payload(policy: dict) -> dict[str, Any]:
    meta = policy.get("_attribution_policy_meta") or policy.get("attribution_policy_meta") or {}
    return {
        "dynamic_mode": str(policy.get("_dynamic_mode") or policy.get("dynamic_mode") or "").strip(),
        "signal_weights": policy.get("_signal_weights") or policy.get("signal_weights") or {},
        "attribution_signal_weights": policy.get("_attribution_signal_weights")
        or policy.get("attribution_signal_weights")
        or {},
        "attribution_policy_meta": meta,
        "selection_action_count": int(meta.get("selection_action_count") or 0),
        "selection_action_summary": str(meta.get("selection_action_summary") or "").strip(),
        "formal_dynamic_allowed": meta.get("formal_dynamic_allowed"),
        "policy_weight_active_scope": str(meta.get("policy_weight_active_scope") or meta.get("active_scope") or ""),
        "execution_policy": str(meta.get("execution_policy") or ""),
        "next_action": str(meta.get("next_action") or ""),
    }


def stage_name(ctx: Any, code: str) -> str:
    if code in ctx.candidate_entry_map:
        state = str(ctx.candidate_entry_map[code].get("state", "") or "").strip()
        if state:
            return state
    return "Markup" if code in ctx.markup_symbols else str(ctx.accum_stage_map.get(code, "") or "").strip()


def display_score(ctx: Any, selection: FunnelAiSelection, code: str) -> float:
    trigger_score = candidate_score_value(ctx.code_to_total_score.get(code))
    selection_score = candidate_score_value(selection.score_map.get(code))
    return max(trigger_score, selection_score)


def display_score_map(ctx: Any, selection: FunnelAiSelection) -> dict[str, float]:
    out = {str(code): candidate_score_value(score) for code, score in (selection.score_map or {}).items()}
    for code in selection.selected_for_ai:
        out[str(code)] = display_score(ctx, selection, str(code))
    return out


def legacy_display_score(ctx: Any, code: str) -> float:
    fallback = (ctx.metrics.get("layer3_score_map", {}) or {}).get(code, 0.0)
    return candidate_score_value(ctx.code_to_total_score.get(code)) or candidate_score_value(fallback)


def legacy_selection_source(ctx: Any, code: str) -> str:
    if code in getattr(ctx, "mainline_candidate_set", set()):
        return "mainline"
    if code in ctx.candidate_entry_map:
        return "alpha_candidate"
    if code in ctx.strategic_l2_bypass_set:
        return "strategic_l2_bypass"
    if code in ctx.l2_bypass_set:
        return "l2_bypass"
    return "l4_hit"


def selection_source(ctx: Any, code: str) -> str:
    if code in getattr(ctx, "mainline_candidate_set", set()):
        return "mainline"
    if code in ctx.candidate_entry_map:
        return "alpha_candidate"
    if code in ctx.strategic_l2_bypass_set:
        return "strategic_l2_bypass"
    if code in ctx.l2_bypass_set:
        return "l2_bypass"
    if code in ctx.formal_hit_set:
        return "l4_hit"
    if code in ctx.markup_symbols:
        return "markup"
    return "accum_c" if stage_name(ctx, code) == "Accum_C" else "l3_fill"


def selected_track(selection: FunnelAiSelection, code: str) -> str:
    if code in selection.trend_selected:
        return "Trend"
    return "Accum" if code in selection.accum_selected else ""
