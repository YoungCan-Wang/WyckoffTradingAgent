"""AI candidate selection workflow for the A-share funnel."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from core.ai_candidate_allocation import (
    AiCandidateAllocationConfig,
    allocate_ai_candidates,
    resolve_ai_candidate_policy,
)
from core.candidate_policy import candidate_score_value
from core.dynamic_policy import (
    DynamicPolicyConfig,
    build_signal_weight_map,
    dynamic_policy_horizon,
    dynamic_policy_mode,
    filter_triggers_by_registry,
    merge_signal_weight_maps,
    resolve_dynamic_candidate_policy,
)
from core.funnel_selection import (
    promote_bypass_groups,
    promote_l2_bypass_for_ai,
    should_force_quota_selection,
    split_selected_tracks,
)
from core.funnel_theme import apply_theme_bonus_to_scores, promote_theme_l4_for_ai
from core.market_trade_mode import resolve_market_trade_mode
from core.strategy_policy_display import policy_weight_rows
from core.wyckoff_engine import FunnelResult
from integrations.supabase_signal_feedback import (
    load_signal_health_snapshot,
    load_signal_registry,
    upsert_policy_shadow_run,
)
from utils.trading_clock import CN_TZ
from workflows.ai_candidate_allocation_config import ai_candidate_allocation_config_from_env
from workflows.dynamic_policy_config import dynamic_policy_config_from_env
from workflows.funnel_settings import (
    FUNNEL_AI_SELECTION_MODE,
    FUNNEL_DEFENSIVE_FORCE_QUOTA,
    FUNNEL_FULL_FORMAL_L4_MAX,
    FUNNEL_L2_BYPASS_AI_CAP,
    FUNNEL_L2_BYPASS_AI_ENABLED,
    FUNNEL_MAINLINE_MAX_AI_CANDIDATES,
    FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP,
    FUNNEL_STRATEGIC_L2_BYPASS_AI_ENABLED,
    FUNNEL_THEME_RADAR_PROMOTE_CAP,
)
from workflows.strategy_attribution_policy import attribution_weights_for_funnel, load_attribution_policy_snapshot

SHADOW_POLICY_SCHEMA_VERSION = "shadow_policy_v2"


@dataclass(frozen=True)
class FunnelAiSelection:
    selected_for_ai: list[str]
    trend_selected: list[str]
    accum_selected: list[str]
    score_map: dict[str, float]
    ai_policy: dict
    theme_promoted_count: int
    mainline_promoted_count: int = 0


def select_base_ai_candidates(
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
    benchmark_context: dict,
    formal_sorted_codes: list[str],
    code_to_best_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    *,
    full_mode_enabled: bool,
) -> tuple[list[str], list[str], list[str], dict[str, float], dict, bool]:
    dynamic_config = dynamic_policy_config_from_env()
    allocation_config = ai_candidate_allocation_config_from_env()
    trade_mode = resolve_market_trade_mode(regime)
    if not trade_mode.allow_ai_review:
        ai_policy = resolve_ai_candidate_policy(regime, override_total_cap=0, config=allocation_config)
        ai_policy.update(
            {
                "trade_mode": trade_mode.mode,
                "trade_action": trade_mode.action,
                "trade_gate_reason": trade_mode.reason,
            }
        )
        print(f"[funnel] 市场交易闸门: {trade_mode.regime} -> {trade_mode.action}")
        return [], [], [], {}, ai_policy, False
    force_quota = should_force_quota_selection(
        regime,
        full_mode_enabled,
        defensive_force_quota=FUNNEL_DEFENSIVE_FORCE_QUOTA,
    )
    if full_mode_enabled and not trade_mode.allow_full_l4:
        force_quota = True
    use_full_ai_selection = full_mode_enabled and not force_quota
    if force_quota:
        print(f"[funnel] 市场模式 {trade_mode.mode}: {regime} 强制从 full_l4 切换为 quota 选股")
    if use_full_ai_selection:
        result = full_formal_ai_selection(formal_sorted_codes, code_to_best_score, code_to_trigger_keys)
        if dynamic_policy_mode(dynamic_config) == "shadow":
            attach_shadow_policy(
                result[4],
                _load_dynamic_policy_context(str(regime), benchmark_context, dynamic_config, allocation_config),
            )
        return (*result, True)
    trend_selected, accum_selected, score_map, ai_policy = _allocate_candidates_for_ai(
        metrics,
        triggers,
        l3_ranked_symbols,
        str(regime),
        sector_map,
        benchmark_context,
        dynamic_config,
        allocation_config,
    )
    return trend_selected + accum_selected, trend_selected, accum_selected, score_map, ai_policy, False


def promote_review_candidates(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    pools: dict[str, object],
    code_to_total_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    score_map: dict[str, float],
    ai_policy: dict,
    use_full_ai_selection: bool,
    theme_bonus_map: dict[str, float],
    regime: str,
    capital_migration_bonus_map: dict[str, float] | None = None,
) -> tuple[int, int, int, int]:
    trade_mode = resolve_market_trade_mode(regime)
    if not use_full_ai_selection:
        apply_theme_bonus_to_scores(score_map, theme_bonus_map)
        apply_theme_bonus_to_scores(score_map, capital_migration_bonus_map or {})
    ai_total_cap = int(ai_policy.get("total_cap") or 0)
    promotion_total_cap = None if FUNNEL_AI_SELECTION_MODE == "tradeable_l4" else ai_total_cap
    bypass_added, strategic_added = promote_bypass_groups(
        selected_for_ai,
        trend_selected,
        accum_selected,
        pools,
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
        ai_total_cap=promotion_total_cap,
        bypass_enabled=FUNNEL_L2_BYPASS_AI_ENABLED and trade_mode.allow_bypass_review,
        bypass_cap=FUNNEL_L2_BYPASS_AI_CAP,
        strategic_enabled=FUNNEL_STRATEGIC_L2_BYPASS_AI_ENABLED and trade_mode.allow_bypass_review,
        strategic_cap=FUNNEL_STRATEGIC_L2_BYPASS_AI_CAP,
        regime=regime,
    )
    theme_added = 0
    if trade_mode.allow_theme_promotion:
        theme_added = promote_theme_l4_for_ai(
            selected_for_ai,
            trend_selected,
            accum_selected,
            set(pools["formal_hit"]),
            theme_bonus_map,
            code_to_total_score,
            code_to_trigger_keys,
            score_map,
            promotion_cap=FUNNEL_THEME_RADAR_PROMOTE_CAP,
            total_cap=promotion_total_cap,
        )
    mainline_cap = int(pools.get("mainline_cap") or FUNNEL_MAINLINE_MAX_AI_CANDIDATES)
    mainline_total_cap: int | None = None
    if not trade_mode.allow_recommendation_write:
        mainline_cap = min(mainline_cap, 2)
        mainline_total_cap = 2
    mainline_added = _promote_mainline_for_ai(
        selected_for_ai,
        trend_selected,
        accum_selected,
        pools,
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
        enabled=trade_mode.allow_ai_review,
        cap=mainline_cap,
        total_cap=mainline_total_cap,
    )
    ai_policy["mainline_added_count"] = mainline_added
    return bypass_added, strategic_added, theme_added, mainline_added


def _promote_mainline_for_ai(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    pools: dict[str, object],
    code_to_total_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
    score_map: dict[str, float],
    *,
    enabled: bool,
    cap: int,
    total_cap: int | None = None,
) -> int:
    return promote_l2_bypass_for_ai(
        selected_for_ai,
        trend_selected,
        accum_selected,
        list(pools.get("mainline") or []),
        code_to_total_score,
        code_to_trigger_keys,
        score_map,
        enabled=enabled,
        cap=cap,
        total_cap=total_cap,
    )


def maybe_persist_policy_shadow_run(
    *,
    ai_policy: dict,
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    selected_for_ai: list[str],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
) -> dict:
    if ai_policy.get("_dynamic_mode") != "shadow" or not ai_policy.get("_shadow_policy"):
        return {}
    shadow_trend, shadow_accum, score_map = _shadow_selected_codes(
        metrics,
        triggers,
        l3_ranked_symbols,
        regime,
        sector_map,
        ai_policy,
    )
    shadow_selected = shadow_trend + shadow_accum
    diff_added, diff_removed = selection_diff(selected_for_ai, shadow_selected)
    row = _policy_shadow_row(ai_policy, metrics, selected_for_ai, shadow_selected, diff_added, diff_removed, regime)
    written = upsert_policy_shadow_run(row)
    if written:
        print(
            "[funnel] 动态策略shadow已写入 signal_policy_shadow_runs: "
            f"written={written}, added={len(diff_added)}, removed={len(diff_removed)}"
        )
    else:
        # 原先「已写入 ... written=0」照样打印，读着像正常输出。实测这条就是影子账本
        # 2026-07-04 起停摆两个月没被发现的原因：upsert 是 raise_on_error=False，
        # 缺列异常只进 logger.warning，而这行日志说的是「已写入」。
        print(
            "[funnel] 警告: 动态策略shadow写入失败(0 行落库)，"
            "归因重算会持续报 insufficient_shadow_sample；"
            "检查 signal_policy_shadow_runs 是否缺列(scripts/print_signal_policy_shadow_ddl.py)"
        )
    return _policy_shadow_meta(written, shadow_selected, diff_added, diff_removed, score_map)


def full_formal_ai_selection(
    formal_sorted_codes: list[str],
    code_to_best_score: dict[str, float],
    code_to_trigger_keys: dict[str, list[str]],
) -> tuple[list[str], list[str], list[str], dict[str, float], dict]:
    cap = int(FUNNEL_FULL_FORMAL_L4_MAX)
    selected_for_ai = list(formal_sorted_codes if cap <= 0 else formal_sorted_codes[:cap])
    trend_selected, accum_selected = split_selected_tracks(selected_for_ai, code_to_trigger_keys)
    ai_policy = {
        "total_cap": len(selected_for_ai),
        "trend_quota": len(trend_selected),
        "accum_quota": len(accum_selected),
        "requested_trend_quota": len(trend_selected),
        "requested_accum_quota": len(accum_selected),
        "quota_family": "FULL_FORMAL_L4",
        "formal_l4_total": len(formal_sorted_codes),
        "formal_l4_cap": cap,
        "max_trend_l3_fill": 0,
        "max_accum_l3_fill": 0,
    }
    score_map = {c: candidate_score_value(code_to_best_score.get(c)) for c in selected_for_ai}
    print(
        f"[funnel] AI候选分配完成(full_formal_l4): "
        f"Trend={len(trend_selected)}, Accum={len(accum_selected)}, total={len(selected_for_ai)}, "
        f"formal_total={len(formal_sorted_codes)}, cap={'unlimited' if cap <= 0 else cap}"
    )
    return selected_for_ai, trend_selected, accum_selected, score_map, ai_policy


def attach_shadow_policy(ai_policy: dict, dynamic_ctx: dict) -> None:
    if str(dynamic_ctx.get("mode") or "off") != "shadow" or not dynamic_ctx.get("policy"):
        return
    shadow_policy = dynamic_ctx["policy"]
    ai_policy["_dynamic_mode"] = "shadow"
    ai_policy["_shadow_policy"] = shadow_policy
    ai_policy["_signal_weights"] = dynamic_ctx.get("weights") or {}
    ai_policy["_registry_rows"] = dynamic_ctx.get("registry") or []
    ai_policy["_health_rows"] = dynamic_ctx.get("health") or []
    ai_policy["_attribution_signal_weights"] = dynamic_ctx.get("attribution_weights") or {}
    ai_policy["_attribution_policy_meta"] = dynamic_ctx.get("attribution_policy_meta") or {}
    ai_policy["_pv_policy_shadow"] = dynamic_ctx.get("pv_policy_shadow") or {}
    print(
        "[funnel] 动态策略shadow: "
        f"base Trend={ai_policy['trend_quota']}, Accum={ai_policy['accum_quota']} -> "
        f"shadow Trend={shadow_policy['trend_quota']}, Accum={shadow_policy['accum_quota']}"
    )


def selection_diff(base_selected: list[str], shadow_selected: list[str]) -> tuple[list[str], list[str]]:
    base_set = set(base_selected)
    shadow_set = set(shadow_selected)
    return ([c for c in shadow_selected if c not in base_set], [c for c in base_selected if c not in shadow_set])


def _load_dynamic_policy_context(
    regime: str,
    benchmark_context: dict,
    dynamic_config: DynamicPolicyConfig,
    allocation_config: AiCandidateAllocationConfig,
) -> dict:
    mode = dynamic_policy_mode(dynamic_config)
    pv_policy_shadow = benchmark_context.get("market_pv_policy_shadow") or {}
    if mode == "off":
        return _dynamic_policy_fallback(mode, pv_policy_shadow)
    try:
        health_rows = load_signal_health_snapshot(market="cn")
        registry_rows = load_signal_registry(market="cn")
    except Exception as exc:
        print(f"[funnel] 动态策略上下文加载失败，降级为静态: {exc}")
        return _dynamic_policy_fallback("off", pv_policy_shadow)
    horizon = dynamic_policy_horizon(dynamic_config)
    feedback_weights = build_signal_weight_map(health_rows, registry_rows, regime=regime, horizon_days=horizon)
    attribution_snapshot = load_attribution_policy_snapshot(
        market="cn", log_fn=lambda message: print(f"[funnel] {message}")
    )
    attribution_weights = _effective_attribution_weights(attribution_snapshot, mode)
    weights = merge_signal_weight_maps(feedback_weights, attribution_weights)
    base_policy = resolve_ai_candidate_policy(regime, config=allocation_config)
    policy = resolve_dynamic_candidate_policy(
        base_policy,
        weights,
        breadth=(benchmark_context.get("breadth") or {}),
    )
    if health_rows or registry_rows or attribution_weights:
        print(
            "[funnel] 动态策略上下文: "
            f"mode={mode}, horizon={horizon}, weights={weights or {}}, "
            f"TrendWeight={policy.get('trend_health_weight', 1)}, "
            f"AccumWeight={policy.get('accum_health_weight', 1)}"
        )
    return {
        "mode": mode,
        "horizon_days": horizon,
        "health": health_rows,
        "registry": registry_rows,
        "weights": weights,
        "attribution_weights": attribution_weights,
        "attribution_policy_meta": attribution_snapshot.as_dict(),
        "policy": policy,
        "pv_policy_shadow": pv_policy_shadow,
    }


def _effective_attribution_weights(attribution_snapshot: Any, mode: str) -> dict[str, float]:
    return attribution_weights_for_funnel(
        attribution_snapshot,
        mode=mode,
        log_fn=lambda message: print(f"[funnel] {message}"),
    )


def _dynamic_policy_fallback(mode: str, pv_policy_shadow: dict) -> dict:
    return {
        "mode": mode,
        "health": [],
        "registry": [],
        "weights": {},
        "attribution_weights": {},
        "attribution_policy_meta": {},
        "policy": None,
        "pv_policy_shadow": pv_policy_shadow,
    }


def _candidate_result(metrics: dict, triggers: dict[str, list[tuple[str, float]]]) -> FunnelResult:
    return FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=metrics.get("layer3_symbols", []) or [],
        top_sectors=[],
        triggers=triggers,
        stage_map=metrics.get("accum_stage_map", {}) or {},
        markup_symbols=metrics.get("markup_symbols", []) or [],
        exit_signals=metrics.get("exit_signals", {}) or {},
        channel_map=metrics.get("layer2_channel_map", {}) or {},
        leader_radar_symbols=metrics.get("leader_radar_symbols", []) or [],
        leader_radar_rows=metrics.get("leader_radar_rows", []) or [],
        candidate_entries=metrics.get("candidate_entries", []) or [],
        layer3_score_map=metrics.get("layer3_score_map", {}) or {},
    )


def _allocate_candidates_for_ai(
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
    benchmark_context: dict,
    dynamic_config: DynamicPolicyConfig,
    allocation_config: AiCandidateAllocationConfig,
) -> tuple[list[str], list[str], dict[str, float], dict]:
    dynamic_ctx = _load_dynamic_policy_context(str(regime), benchmark_context, dynamic_config, allocation_config)
    dynamic_mode = str(dynamic_ctx.get("mode") or "off")
    allocation_triggers = triggers
    if dynamic_mode == "on":
        allocation_triggers = filter_triggers_by_registry(triggers, dynamic_ctx.get("registry", []) or [])
    mock_result = _candidate_result(metrics, allocation_triggers)
    alloc_started = time.monotonic()
    dynamic_policy = dynamic_ctx.get("policy") if dynamic_mode == "on" else None
    trend_selected, accum_selected, score_map = allocate_ai_candidates(
        mock_result,
        l3_ranked_symbols,
        regime,
        sector_map=sector_map,
        max_per_sector=allocation_config.max_per_sector,
        policy_override=dynamic_policy,
        signal_weight_map=(dynamic_ctx.get("weights") or {}) if dynamic_mode == "on" else None,
        allocation_config=allocation_config,
    )
    ai_policy = dynamic_policy or resolve_ai_candidate_policy(regime, config=allocation_config)
    attach_shadow_policy(ai_policy, dynamic_ctx)
    alloc_elapsed = time.monotonic() - alloc_started
    print(
        f"[funnel] AI候选分配完成: trend={len(trend_selected)}, accum={len(accum_selected)}, "
        f"elapsed={alloc_elapsed:.3f}s"
    )
    return trend_selected, accum_selected, score_map, ai_policy


def _shadow_selected_codes(
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    l3_ranked_symbols: list[str],
    regime: str,
    sector_map: dict[str, str],
    ai_policy: dict,
) -> tuple[list[str], list[str], dict[str, float]]:
    shadow_triggers = filter_triggers_by_registry(triggers, ai_policy.get("_registry_rows", []) or [])
    trend, accum, score_map = allocate_ai_candidates(
        _candidate_result(metrics, shadow_triggers),
        l3_ranked_symbols,
        regime,
        sector_map=sector_map,
        max_per_sector=int(ai_policy.get("max_per_sector") or 2),
        policy_override=ai_policy.get("_shadow_policy"),
        signal_weight_map=ai_policy.get("_signal_weights") or {},
    )
    return trend, accum, score_map


def _policy_shadow_row(
    ai_policy: dict,
    metrics: dict,
    selected_for_ai: list[str],
    shadow_selected: list[str],
    diff_added: list[str],
    diff_removed: list[str],
    regime: str,
) -> dict:
    registry_rows = ai_policy.get("_registry_rows") or []
    health_rows = ai_policy.get("_health_rows") or []
    base_policy = _public_policy(ai_policy)
    shadow_policy = _public_policy(ai_policy.get("_shadow_policy") or {})
    return {
        "market": "cn",
        "trade_date": str(metrics.get("end_trade_date") or date.today().isoformat()),
        "regime": str(regime or "NEUTRAL").strip().upper() or "NEUTRAL",
        "schema_version": SHADOW_POLICY_SCHEMA_VERSION,
        "snapshot_level": "summary",
        "base_policy": base_policy,
        "shadow_policy": shadow_policy,
        "signal_weights": ai_policy.get("_signal_weights") or {},
        "attribution_signal_weights": ai_policy.get("_attribution_signal_weights") or {},
        "attribution_policy_meta": ai_policy.get("_attribution_policy_meta") or {},
        "base_selected": selected_for_ai,
        "shadow_selected": shadow_selected,
        "diff_added": diff_added,
        "diff_removed": diff_removed,
        "selection_summary": _selection_summary(selected_for_ai, shadow_selected, diff_added, diff_removed),
        "policy_summary": _policy_summary(
            base_policy,
            shadow_policy,
            ai_policy.get("_signal_weights") or {},
            ai_policy.get("_attribution_signal_weights") or {},
            ai_policy.get("_attribution_policy_meta") or {},
        ),
        "registry_summary": _registry_summary(registry_rows),
        "health_summary": _health_summary(health_rows),
        "registry_snapshot": [],
        "health_snapshot": [],
        "updated_at": datetime.now(CN_TZ).isoformat(),
    }


def _policy_shadow_meta(
    written: bool,
    shadow_selected: list[str],
    diff_added: list[str],
    diff_removed: list[str],
    score_map: dict[str, float],
) -> dict:
    return {
        "shadow_table": "signal_policy_shadow_runs",
        "shadow_written": written,
        "shadow_added_count": len(diff_added),
        "shadow_removed_count": len(diff_removed),
        "shadow_selected": shadow_selected,
        "shadow_added": diff_added,
        "shadow_removed": diff_removed,
        "shadow_score_map": {code: candidate_score_value(score_map.get(code)) for code in shadow_selected},
    }


def _public_policy(policy: dict) -> dict:
    return {key: value for key, value in policy.items() if not str(key).startswith("_")}


def _selection_summary(
    selected_for_ai: list[str],
    shadow_selected: list[str],
    diff_added: list[str],
    diff_removed: list[str],
) -> dict:
    base_set = set(selected_for_ai)
    shadow_set = set(shadow_selected)
    overlap = len(base_set & shadow_set)
    return {
        "base_count": len(selected_for_ai),
        "shadow_count": len(shadow_selected),
        "overlap_count": overlap,
        "diff_added_count": len(diff_added),
        "diff_removed_count": len(diff_removed),
        "jaccard": round(overlap / max(len(base_set | shadow_set), 1), 4),
    }


def _policy_summary(
    base_policy: dict,
    shadow_policy: dict,
    signal_weights: dict,
    attribution_weights: dict | None = None,
    attribution_policy_meta: dict | None = None,
) -> dict:
    return {
        "base": _policy_core(base_policy),
        "shadow": _policy_core(shadow_policy),
        "signal_weight_count": len(signal_weights),
        "attribution_weight_count": len(attribution_weights or {}),
        "attribution_policy_meta": attribution_policy_meta or {},
        "downweighted_signals": _weighted_signals(signal_weights, upper_bound=0.999),
        "upweighted_signals": _weighted_signals(signal_weights, lower_bound=1.001),
    }


def _policy_core(policy: dict) -> dict:
    keys = (
        "quota_family",
        "total_cap",
        "trend_quota",
        "accum_quota",
        "requested_trend_quota",
        "requested_accum_quota",
    )
    return {key: policy.get(key) for key in keys if policy.get(key) is not None}


def _weighted_signals(weights: dict, *, lower_bound: float = 0.0, upper_bound: float = float("inf")) -> list[dict]:
    rows = []
    for item in policy_weight_rows(weights):
        weight = _candidate_score_value(item.get("weight"))
        if lower_bound <= weight <= upper_bound:
            rows.append(
                {
                    "signal_type": str(item.get("signal_type") or ""),
                    "key": str(item.get("key") or ""),
                    "label": str(item.get("label") or ""),
                    "scope": item.get("scope") or {},
                    "weight": weight,
                }
            )
    return sorted(rows, key=lambda row: (row["weight"], row["label"] or row["signal_type"]))[:20]


def _registry_summary(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    changed: list[dict] = []
    for row in rows:
        status = str(row.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"
        by_status[status] = by_status.get(status, 0) + 1
        weight = _candidate_score_value(row.get("weight_multiplier"), default=1.0)
        if status != "ACTIVE" or abs(weight - 1.0) > 0.0001:
            changed.append(_signal_policy_item(row, state=status, weight=weight))
    return {"count": len(rows), "by_status": by_status, "changed": changed[:30]}


def _health_summary(rows: list[dict]) -> dict:
    by_state: dict[str, int] = {}
    changed: list[dict] = []
    for row in rows:
        state = str(row.get("health_state") or "UNKNOWN").strip().upper() or "UNKNOWN"
        by_state[state] = by_state.get(state, 0) + 1
        weight = _candidate_score_value(row.get("weight_multiplier"), default=1.0)
        if state not in {"HEALTHY", "NEUTRAL"} or abs(weight - 1.0) > 0.0001:
            changed.append(_signal_policy_item(row, state=state, weight=weight))
    return {"count": len(rows), "by_state": by_state, "changed": changed[:30]}


def _signal_policy_item(row: dict, *, state: str, weight: float) -> dict:
    return {
        "signal_type": str(row.get("signal_type") or ""),
        "regime": str(row.get("regime") or "ALL"),
        "horizon_days": int(row.get("horizon_days") or 0),
        "state": state,
        "weight": weight,
        "sample_count": int(row.get("sample_count") or 0),
        "avg_return_pct": row.get("avg_return_pct"),
    }


def _candidate_score_value(raw: object, *, default: float = 0.0) -> float:
    if raw is None or raw == "":
        return default
    return candidate_score_value(raw)
