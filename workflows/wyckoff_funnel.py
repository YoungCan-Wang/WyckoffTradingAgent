"""
Wyckoff Funnel 定时任务：5 层漏斗筛选 → 多渠道推送

Layer 1: 剥离垃圾（ST/非目标板块/市值/成交额）
Layer 2: 八通道甄选（主升/潜伏/吸筹/地量/暗中护盘/趋势延续/加速突破/点火破局）
Layer 2.5: Markup 加速检测
Layer 3: 板块共振（行业 Top-N）
Layer 4: 威科夫狙击（Spring / SOS / LPS / Effort vs Result）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import pandas as pd

from core.candidate_policy import (
    apply_loss_guard,
    candidate_score_value,
    cap_quality_candidates,
    is_tradeable_l4_trigger_combo,
)
from core.candidate_tracks import candidate_entry_track
from core.capital_migration import build_capital_migration_report
from core.cn_boards import is_main_or_chinext, is_star_or_bse
from core.funnel_etf import etf_metrics
from core.funnel_selection import split_selected_tracks
from core.theme_activity import summarize_theme_activity
from core.theme_radar import summarize_theme_radar
from core.wyckoff_engine import (
    FunnelConfig,
    layer4_triggers,
)
from integrations.ths_hot_concept import merge_concept_heat, summarize_ths_hot_events
from utils.env import env_bool
from utils.progress import report_progress as _report_progress
from workflows.candidate_policy_config import candidate_policy_config_from_env
from workflows.funnel_ai_selection import (
    FunnelAiSelection,
    maybe_persist_policy_shadow_run,
    promote_review_candidates,
    select_base_ai_candidates,
)
from workflows.funnel_candidates import (
    FunnelCandidateOutputs,
    FunnelStrategicBypass,
    build_candidate_outputs,
    build_l2_bypass_pool,
    build_strategic_bypass_from_theme,
    trigger_hit_codes,
)
from workflows.funnel_data import (
    FunnelReferenceData,
    FunnelSymbolPool,
    prepare_funnel_job_data,
)
from workflows.funnel_data_quality import build_funnel_data_quality, build_layer_rejections
from workflows.funnel_delivery import deliver_funnel_selection
from workflows.funnel_layers import FunnelLayerOutputs, run_base_funnel_layers
from workflows.funnel_render_context import FunnelRenderContext, build_render_context
from workflows.funnel_settings import (
    FUNNEL_AI_SELECTION_MODE,
    FUNNEL_CARD_STYLE,
    FUNNEL_MARKET_MIX_GUARD_ENABLED,
    FUNNEL_MARKET_MIX_MAX_ADD,
    FUNNEL_MARKET_MIX_MIN_SCORE,
)

logger = logging.getLogger(__name__)

ENFORCE_TARGET_TRADE_DATE = env_bool("FUNNEL_ENFORCE_TARGET_TRADE_DATE", True)


@dataclass(frozen=True)
class FunnelMetricsInputs:
    cfg: FunnelConfig
    pool: FunnelSymbolPool
    window: object
    fetch_stats: dict
    snapshot_dir: str
    layers: FunnelLayerOutputs
    ref_data: FunnelReferenceData
    bench_df: pd.DataFrame | None
    etf_symbols: list[str]
    etf_sector_map: dict[str, str]
    etf_df_map: dict[str, pd.DataFrame]
    etf_l2_passed: list[str]
    etf_candidates: list[dict]
    l2_bypass_pool: list[str]
    l2_bypass_triggers: dict[str, list[tuple[str, float]]]
    strategic: FunnelStrategicBypass
    candidates: FunnelCandidateOutputs
    external_seed_cfg: ExternalSeedConfig
    external_added_to_pool: int
    external_seed_review: dict
    benchmark_context: dict
    all_df_map: dict[str, pd.DataFrame]
    financial_map: dict[str, dict]
    financial_metrics_requested: bool = True


@dataclass(frozen=True)
class FunnelRunArtifacts:
    layers: FunnelLayerOutputs
    l2_bypass_pool: list[str]
    l2_bypass_triggers: dict[str, list[tuple[str, float]]]
    strategic: FunnelStrategicBypass
    candidates: FunnelCandidateOutputs
    external_seed_review: dict


from tools.external_seeds import (
    ExternalSeedConfig,
    build_external_seed_rows,
)


def _build_external_seed_review(
    seed_cfg: ExternalSeedConfig,
    trade_date: str,
    l1_passed: list[str],
    l2_passed: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    channel_map: dict[str, str],
    market_cap_map: dict[str, float],
    name_map: dict[str, str],
    sector_map: dict[str, str],
) -> dict:
    empty = {"triggers": {}, "confirmed": [], "watch": [], "rows": []}
    if not seed_cfg.enabled:
        return empty
    l1_set, l2_set = set(l1_passed), set(l2_passed)
    review_codes = [c for c in seed_cfg.symbols if c in l1_set and c not in l2_set]
    triggers = {}
    if seed_cfg.allow_l2_bypass_review and review_codes:
        triggers = layer4_triggers(review_codes, df_map, cfg, channel_map=channel_map, market_cap_map=market_cap_map)
    confirmed = sorted(trigger_hit_codes(triggers))
    rows = build_external_seed_rows(
        seed_cfg,
        trade_date,
        l1_codes=l1_passed,
        l2_codes=l2_passed,
        l4_triggers=triggers,
        name_map=name_map,
        sector_map=sector_map,
    )
    watch = [c for c in review_codes if c not in set(confirmed)]
    return {"triggers": triggers, "confirmed": confirmed, "watch": watch, "rows": rows}


def _log_external_seed_review(seed_cfg: ExternalSeedConfig, review: dict) -> None:
    if not seed_cfg.enabled:
        return
    print(f"[funnel] 外部观察确认: L4={len(review.get('confirmed') or [])}, watch={len(review.get('watch') or [])}")


def _external_seed_metrics(
    seed_cfg: ExternalSeedConfig,
    added_to_pool: int,
    l1_passed: list[str],
    l2_passed: list[str],
    review: dict,
) -> dict:
    l1_set, l2_set = set(l1_passed), set(l2_passed)
    return {
        "external_seed_source": seed_cfg.source,
        "external_seed_count": len(seed_cfg.symbols) if seed_cfg.enabled else 0,
        "external_seed_added_to_pool": added_to_pool,
        "external_seed_l1_codes": [c for c in seed_cfg.symbols if c in l1_set],
        "external_seed_l2_codes": [c for c in seed_cfg.symbols if c in l2_set],
        "external_seed_rejected_l1_codes": [c for c in seed_cfg.symbols if c not in l1_set],
        "external_seed_l4_triggers": review.get("triggers") or {},
        "external_seed_l4_confirmed_codes": review.get("confirmed") or [],
        "external_seed_watch_codes": review.get("watch") or [],
        "external_seed_observation_rows": review.get("rows") or [],
        "external_seed_watch_ttl_days": seed_cfg.watch_ttl_days,
        "external_seed_retention_days": seed_cfg.retention_days,
    }


def _candidate_entry_type_counts(candidate_entries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in candidate_entries:
        entry_type = str(item.get("entry_type", "") or "unknown")
        counts[entry_type] = counts.get(entry_type, 0) + 1
    return counts


def _latest_close_map(df_map: dict[str, pd.DataFrame]) -> dict[str, float]:
    result: dict[str, float] = {}
    for sym, df in df_map.items():
        try:
            close_series = pd.to_numeric(df.get("close"), errors="coerce")
            if close_series is None or close_series.empty:
                continue
            last_close = close_series.iloc[-1]
            if pd.notna(last_close):
                result[str(sym).strip()] = float(last_close)
        except Exception:
            logger.debug("close price parse failed for %s", sym, exc_info=True)
    return result


def _selection_mode_flags() -> tuple[bool, bool, bool]:
    full_formal = FUNNEL_AI_SELECTION_MODE in {"all_formal_l4", "all_l4", "full_formal_l4", "full_l4"}
    legacy_selection = FUNNEL_AI_SELECTION_MODE in {"legacy_full_hits", "legacy_hits", "all_hits", "classic"}
    legacy_card = FUNNEL_CARD_STYLE in {"legacy", "legacy_compact", "classic", "v1"}
    return full_formal, legacy_selection, legacy_card


def _apply_data_quality_mode(selection: FunnelAiSelection, metrics: dict) -> FunnelAiSelection:
    quality = metrics.get("data_quality") or {}
    if quality.get("trade_readiness") != "observe_only":
        return selection
    policy = {
        **selection.ai_policy,
        "data_quality_status": str(quality.get("status") or "degraded"),
        "trade_readiness": "observe_only",
    }
    return replace(selection, ai_policy=policy)


def _select_run_ai_candidates(
    ctx: FunnelRenderContext,
    l3_ranked_symbols: list[str],
    full_mode_enabled: bool,
) -> FunnelAiSelection:
    selected_for_ai, trend_selected, accum_selected, score_map, ai_policy, use_full_ai_selection = (
        _select_base_for_context(ctx, l3_ranked_symbols, full_mode_enabled)
    )
    selected_for_ai, trend_selected, accum_selected = _expand_quality_first_pool(
        ctx,
        selected_for_ai,
        score_map,
        ai_policy,
    )
    strategic_accum_codes = {
        str(code).strip()
        for code, stage in ctx.strategic_l2_bypass_stage_map.items()
        if str(stage or "").strip() in {"Accum_B", "Accum_C"}
    }
    _bypass_added, _strategic_added, theme_promoted_count, mainline_promoted_count = promote_review_candidates(
        selected_for_ai,
        trend_selected,
        accum_selected,
        {
            "l2_bypass": ctx.l2_bypass_pool,
            "strategic_l2_bypass": ctx.strategic_l2_bypass_pool,
            "strategic_accum": strategic_accum_codes,
            "formal_hit": ctx.formal_hit_set,
            "mainline": ctx.mainline_tradeable_codes,
            "mainline_cap": ctx.metrics.get("mainline_ai_cap", 3),
        },
        ctx.code_to_total_score,
        ctx.code_to_trigger_keys,
        score_map,
        ai_policy,
        use_full_ai_selection,
        ctx.theme_bonus_map,
        ctx.regime,
        capital_migration_bonus_map=ctx.capital_migration_bonus_map,
    )
    selected_for_ai, trend_selected, accum_selected = _apply_ai_post_filters(
        ctx, selected_for_ai, trend_selected, accum_selected, score_map, ai_policy
    )
    shadow_meta = maybe_persist_policy_shadow_run(
        ai_policy=ai_policy,
        metrics=ctx.metrics,
        triggers=ctx.formal_triggers,
        selected_for_ai=selected_for_ai,
        l3_ranked_symbols=l3_ranked_symbols,
        regime=ctx.regime,
        sector_map=ctx.sector_map,
    )
    ai_policy.update(shadow_meta)
    return FunnelAiSelection(
        selected_for_ai,
        trend_selected,
        accum_selected,
        score_map,
        ai_policy,
        theme_promoted_count,
        mainline_promoted_count,
    )


def _select_base_for_context(
    ctx: FunnelRenderContext,
    l3_ranked_symbols: list[str],
    full_mode_enabled: bool,
) -> tuple[list[str], list[str], list[str], dict[str, float], dict, bool]:
    return select_base_ai_candidates(
        ctx.metrics,
        ctx.formal_triggers,
        l3_ranked_symbols,
        ctx.regime,
        ctx.sector_map,
        ctx.benchmark_context,
        ctx.formal_sorted_codes,
        ctx.code_to_total_score,
        ctx.code_to_trigger_keys,
        full_mode_enabled=full_mode_enabled,
    )


def _apply_ai_post_filters(
    ctx: FunnelRenderContext,
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    score_map: dict[str, float],
    ai_policy: dict,
) -> tuple[list[str], list[str], list[str]]:
    mainline_codes = set(getattr(ctx, "mainline_tradeable_codes", None) or [])
    selected_for_ai, trend_selected, accum_selected, dropped = apply_loss_guard(
        selected_for_ai,
        trend_selected,
        accum_selected,
        regime=ctx.regime,
        code_to_trigger_keys=ctx.code_to_trigger_keys,
        code_to_total_score=ctx.code_to_total_score,
        channel_map=ctx.l2_channel_map,
        df_map=ctx.all_df_map,
        config=candidate_policy_config_from_env(),
        mainline_codes=mainline_codes,
    )
    if dropped:
        ai_policy["loss_guard_dropped"] = dropped
        print(f"[funnel] loss guard过滤候选: {dropped}")
    _sync_selected_score_map(selected_for_ai, score_map, ctx.code_to_total_score)
    min_score = float(ctx.metrics.get("min_funnel_score", 0.0) or 0.0)
    if score_map and min_score > 0:
        before = len(selected_for_ai)
        selected_for_ai = [c for c in selected_for_ai if candidate_score_value(score_map.get(c)) >= min_score]
        selected_set = set(selected_for_ai)
        trend_selected = [c for c in trend_selected if c in selected_set]
        accum_selected = [c for c in accum_selected if c in selected_set]
        dropped_count = before - len(selected_for_ai)
        if dropped_count:
            print(f"[funnel] min_funnel_score={min_score} 过滤掉 {dropped_count} 只低质量候选")
    selected_for_ai, trend_selected, accum_selected = _apply_market_mix_guard(
        ctx, selected_for_ai, trend_selected, accum_selected, score_map, ai_policy
    )
    selected_for_ai, cap_dropped, sector_dropped = cap_quality_candidates(
        selected_for_ai,
        score_map,
        getattr(ctx, "sector_map", {}),
        total_cap=int(ai_policy.get("total_cap") or 8),
        max_per_sector=int(ai_policy.get("max_per_sector") or 2),
    )
    ai_policy["quality_eligible_before_cap"] = len(selected_for_ai) + len(cap_dropped) + len(sector_dropped)
    ai_policy["total_cap_dropped"] = cap_dropped
    ai_policy["sector_cap_dropped"] = sector_dropped
    ai_policy["final_selected_count"] = len(selected_for_ai)
    trend_selected, accum_selected = split_selected_tracks(selected_for_ai, ctx.code_to_trigger_keys)
    return (
        selected_for_ai,
        trend_selected,
        accum_selected,
    )


def _expand_quality_first_pool(
    ctx: FunnelRenderContext,
    selected_for_ai: list[str],
    score_map: dict[str, float],
    ai_policy: dict,
) -> tuple[list[str], list[str], list[str]]:
    if FUNNEL_AI_SELECTION_MODE != "tradeable_l4" or int(ai_policy.get("total_cap") or 0) <= 0:
        trend, accum = split_selected_tracks(selected_for_ai, ctx.code_to_trigger_keys)
        return selected_for_ai, trend, accum
    pool = list(selected_for_ai)
    for code in list(ctx.formal_sorted_codes) + list(ctx.candidate_entry_map):
        if code in pool or not is_tradeable_l4_trigger_combo(ctx.code_to_trigger_keys.get(code, [])):
            continue
        pool.append(code)
        score_map[code] = candidate_score_value(ctx.code_to_total_score.get(code))
    ai_policy["quality_pool_before_guards"] = len(pool)
    trend, accum = split_selected_tracks(pool, ctx.code_to_trigger_keys)
    return pool, trend, accum


def _apply_market_mix_guard(
    ctx: FunnelRenderContext,
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    score_map: dict[str, float],
    ai_policy: dict,
) -> tuple[list[str], list[str], list[str]]:
    if (
        not FUNNEL_MARKET_MIX_GUARD_ENABLED
        or FUNNEL_MARKET_MIX_MAX_ADD <= 0
        or not selected_for_ai
        or not all(is_star_or_bse(code) for code in selected_for_ai)
    ):
        return selected_for_ai, trend_selected, accum_selected
    alternatives, best_score = _main_or_chinext_alternatives(ctx, selected_for_ai, score_map)
    if not alternatives:
        ai_policy["market_mix_guard_reason"] = _market_mix_guard_reason(best_score)
        return selected_for_ai, trend_selected, accum_selected
    additions = []
    replacements = []
    cap = _market_mix_total_cap(ai_policy)
    slots = None if cap is None else max(cap - len(set(selected_for_ai)), 0)
    for item, score in alternatives[:FUNNEL_MARKET_MIX_MAX_ADD]:
        code = str(item.get("code") or "").strip()
        if code not in selected_for_ai:
            if slots is None or slots > 0:
                selected_for_ai.append(code)
                if slots is not None:
                    slots -= 1
            elif removed := _replace_market_mix_candidate(
                selected_for_ai, trend_selected, accum_selected, score_map, score
            ):
                selected_for_ai.append(code)
                replacements.append({"removed": removed, "added": code})
            else:
                continue
            _append_market_mix_track(code, item, trend_selected, accum_selected)
            score_map[code] = score
            additions.append(code)
    if not additions:
        ai_policy["market_mix_guard_reason"] = _market_mix_full_cap_reason(best_score)
        return selected_for_ai, trend_selected, accum_selected
    ai_policy["market_mix_guard_added"] = additions
    if replacements:
        ai_policy["market_mix_guard_replaced"] = replacements
    print(f"[funnel] 市场均衡补入主板/创业候选: {additions}")
    return selected_for_ai, trend_selected, accum_selected


def _main_or_chinext_alternatives(
    ctx: FunnelRenderContext,
    selected_for_ai: list[str],
    score_map: dict[str, float],
) -> tuple[list[tuple[dict, float]], float | None]:
    selected = set(selected_for_ai)
    rows: list[tuple[str, float, dict]] = []
    best_score: float | None = None
    for item in ctx.candidate_entries:
        score = _market_mix_candidate_score(item, selected, ctx.code_to_total_score, score_map)
        if score is None:
            continue
        best_score = score if best_score is None else max(best_score, score)
        if score >= FUNNEL_MARKET_MIX_MIN_SCORE:
            code = str(item.get("code") or "").strip()
            rows.append((code, score, item))
            selected.add(code)
    rows.sort(key=lambda row: (-row[1], row[0]))
    return [(item, score) for _code, score, item in rows], best_score


def _market_mix_candidate_score(
    item: dict,
    selected: set[str],
    code_to_total_score: dict[str, float],
    score_map: dict[str, float],
) -> float | None:
    code = str(item.get("code") or "").strip()
    if not code or code in selected or not is_main_or_chinext(code):
        return None
    if _candidate_has_hard_risk(item):
        return None
    score = max(candidate_score_value(item.get("score")), candidate_score_value(code_to_total_score.get(code)))
    return max(score, candidate_score_value(score_map.get(code)))


def _market_mix_total_cap(ai_policy: dict) -> int | None:
    cap = int(ai_policy.get("total_cap") or 0)
    return cap if cap > 0 else None


def _replace_market_mix_candidate(
    selected_for_ai: list[str],
    trend_selected: list[str],
    accum_selected: list[str],
    score_map: dict[str, float],
    candidate_score: float,
) -> str:
    removable = [
        (candidate_score_value(score_map.get(code)), idx, code)
        for idx, code in enumerate(selected_for_ai)
        if is_star_or_bse(code)
    ]
    if not removable:
        return ""
    weakest_score, _idx, weakest = min(removable)
    if candidate_score < weakest_score:
        return ""
    selected_for_ai.remove(weakest)
    _remove_market_mix_track(weakest, trend_selected, accum_selected)
    return weakest


def _append_market_mix_track(code: str, item: dict, trend_selected: list[str], accum_selected: list[str]) -> None:
    target = accum_selected if candidate_entry_track(item) == "Accum" else trend_selected
    if code not in target:
        target.append(code)


def _remove_market_mix_track(code: str, trend_selected: list[str], accum_selected: list[str]) -> None:
    if code in trend_selected:
        trend_selected.remove(code)
    if code in accum_selected:
        accum_selected.remove(code)


def _market_mix_guard_reason(best_score: float | None) -> str:
    base = "最终候选集中在科创/北交；"
    if best_score is None:
        return base + "当前没有通过硬风险检查的主板/创业候选。"
    return base + f"主板/创业最高候选分 {best_score:.1f}，低于市场均衡补入门槛 {FUNNEL_MARKET_MIX_MIN_SCORE:.1f}。"


def _market_mix_full_cap_reason(best_score: float | None) -> str:
    base = "最终候选集中在科创/北交；当前 AI 候选已达上限。"
    if best_score is None:
        return base + "没有可用于替换的主板/创业候选。"
    return base + f"主板/创业最高候选分 {best_score:.1f}，未强于现有科创/北交候选。"


def _candidate_has_hard_risk(item: dict) -> bool:
    risk = str(item.get("risk") or "")
    return any(flag in risk for flag in ("鱼尾", "过热不追", "短线过热", "跌破", "长上影", "缩量阴跌"))


def _sync_selected_score_map(
    selected_for_ai: list[str],
    score_map: dict[str, float],
    code_to_total_score: dict[str, float],
) -> None:
    for code in selected_for_ai:
        code_s = str(code).strip()
        if not code_s:
            continue
        score_map[code_s] = max(
            candidate_score_value(score_map.get(code_s)),
            candidate_score_value(code_to_total_score.get(code_s)),
        )


def _build_funnel_metrics(inputs: FunnelMetricsInputs) -> dict:
    ranked_l3_symbols = inputs.candidates.ranked_l3_symbols or inputs.layers.l3_passed
    metrics = {
        **_pool_fetch_metrics(inputs),
        **_data_quality_metrics(inputs),
        **_layer_metrics(
            inputs.layers,
            total_symbols=inputs.layers.rps_universe_count,
            financial_requested=inputs.financial_metrics_requested,
        ),
        **_theme_metrics(inputs, ranked_l3_symbols),
        **_etf_metrics(inputs),
        **_candidate_metrics(inputs, ranked_l3_symbols),
        **_bypass_metrics(inputs),
        **_external_seed_metrics(
            inputs.external_seed_cfg,
            inputs.external_added_to_pool,
            inputs.layers.l1_passed,
            inputs.layers.l2_passed,
            inputs.external_seed_review,
        ),
        **_tail_context_metrics(inputs),
    }
    return metrics


def _pool_fetch_metrics(inputs: FunnelMetricsInputs) -> dict:
    pool = inputs.pool
    return {
        "total_symbols": len(pool.symbols),
        "pool_mode": str(pool.stats.get("pool_mode", "") or ""),
        "pool_main": pool.main_count,
        "pool_chinext": pool.chinext_count,
        "pool_star": pool.star_count,
        "pool_bse": pool.bse_count,
        "pool_merged": pool.merged_count,
        "pool_st_excluded": pool.st_excluded_count,
        "pool_limit": int(pool.stats.get("pool_limit", 0) or 0),
        "pool_batches": pool.total_batches,
        "end_trade_date": inputs.window.end_trade_date.isoformat(),
        "fetch_ok": int(inputs.fetch_stats.get("fetch_ok", len(inputs.all_df_map)) or 0),
        "fetch_fail": int(inputs.fetch_stats.get("fetch_fail", 0) or 0),
        "fetch_date_mismatch": int(inputs.fetch_stats.get("fetch_date_mismatch", 0) or 0),
        "fetch_spot_patched": int(inputs.fetch_stats.get("fetch_spot_patched", 0) or 0),
        "financial_metrics_requested": bool(inputs.financial_metrics_requested),
        "financial_metrics_enabled": bool(inputs.financial_map),
        "financial_metrics_count": len(inputs.financial_map),
        "snapshot_dir": inputs.snapshot_dir,
    }


def _data_quality_metrics(inputs: FunnelMetricsInputs) -> dict:
    window = getattr(inputs, "window", None)
    quality = build_funnel_data_quality(
        inputs.pool.symbols,
        inputs.all_df_map,
        inputs.ref_data.market_cap_map,
        inputs.financial_map,
        financial_requested=inputs.financial_metrics_requested,
        expected_trade_date=getattr(window, "end_trade_date", None),
    )
    coverage = quality["coverage"]
    return {
        "data_quality": quality,
        "data_quality_status": quality["status"],
        "trade_readiness": quality["trade_readiness"],
        "ohlcv_coverage": coverage["ohlcv"],
        "market_cap_coverage": coverage["market_cap"],
        "financial_coverage": coverage["financial"],
        "ohlcv_source_counts": quality["ohlcv_source_counts"],
        "ohlcv_source_ratios": quality["ohlcv_source_ratios"],
    }


def _layer_metrics(layers: FunnelLayerOutputs, *, total_symbols: int, financial_requested: bool = False) -> dict:
    return {
        "layer1": len(layers.l1_passed),
        "layer2": len(layers.l2_passed),
        "layer2_momentum": layers.l2_counts["momentum"],
        "layer2_ambush": layers.l2_counts["ambush"],
        "layer2_accum": layers.l2_counts["accum"],
        "layer2_dry_vol": layers.l2_counts["dry_vol"],
        "layer2_rs_div": layers.l2_counts["rs_div"],
        "layer2_trend_cont": layers.l2_counts["trend_cont"],
        "layer2_sos": layers.l2_counts["sos"],
        "layer2_channel_map": layers.l2_channel_map,
        "layer3": len(layers.l3_passed),
        "top_sectors": layers.top_sectors,
        "sector_rotation": layers.sector_rotation,
        "leader_radar": len(layers.leader_radar_rows),
        "leader_radar_symbols": layers.leader_radar_symbols,
        "leader_radar_rows": layers.leader_radar_rows,
        "mainline_candidates": layers.mainline_candidates,
        "mainline_ai_cap": layers.mainline_ai_cap,
        "by_trigger": {k: len(v) for k, v in layers.triggers.items()},
        "structure_shadow": getattr(layers, "structure_shadow", {}),
        "rps_universe_count": layers.rps_universe_count,
        "layer_rejections": build_layer_rejections(
            total_symbols=total_symbols,
            l1_symbols=layers.l1_passed,
            l2_symbols=layers.l2_passed,
            l3_symbols=layers.l3_passed,
            triggers=layers.triggers,
            financial_requested=financial_requested,
        ),
    }


def _theme_metrics(inputs: FunnelMetricsInputs, ranked_l3_symbols: list[str]) -> dict:
    ref_data = inputs.ref_data
    layers = inputs.layers
    theme_activity = layers.theme_activity
    concept_heat = merge_concept_heat(ref_data.concept_heat, ref_data.event_concept_heat)
    capital_migration = build_capital_migration_report(
        trade_date=inputs.window.end_trade_date.isoformat(),
        concept_heat=concept_heat,
        concept_history=ref_data.concept_heat_history,
        sector_rotation=layers.sector_rotation,
        theme_radar=layers.theme_radar_current,
        theme_activity=theme_activity,
    )
    return {
        "concept_heat": concept_heat[:20],
        "concept_heat_full": concept_heat,
        "event_concept_heat": ref_data.event_concept_heat,
        "ths_hot_events": ref_data.ths_hot_events,
        "ths_hot_events_summary": summarize_ths_hot_events(ref_data.ths_hot_events),
        "theme_activity": theme_activity,
        "theme_activity_summary": summarize_theme_activity(theme_activity),
        "capital_migration": capital_migration,
        "theme_lines": ref_data.hot_concepts,
        "theme_radar": layers.theme_radar,
        "theme_radar_current": layers.theme_radar_current,
        "theme_radar_source": layers.theme_radar_source,
        "candidate_concepts": {s: ref_data.concept_map.get(s, []) for s in ranked_l3_symbols},
    }


def _etf_metrics(inputs: FunnelMetricsInputs) -> dict:
    return {
        "etf_enhancement": etf_metrics(
            inputs.etf_symbols,
            inputs.etf_df_map,
            inputs.etf_l2_passed,
            inputs.etf_sector_map,
            inputs.etf_candidates,
        ),
        "etf_candidates": inputs.etf_candidates,
    }


def _candidate_metrics(inputs: FunnelMetricsInputs, ranked_l3_symbols: list[str]) -> dict:
    candidates = inputs.candidates
    return {
        "layer3_symbols": ranked_l3_symbols,
        "layer3_score_map": candidates.l3_score_map,
        "total_hits": candidates.total_hits,
        "candidate_entries": candidates.candidate_entries,
        "mainline_candidate_entries": candidates.mainline_candidate_entries,
        "lane_candidate_entries": candidates.lane_candidate_entries,
        "candidate_entry_count": len(candidates.candidate_entries),
        "candidate_entry_types": _candidate_entry_type_counts(candidates.candidate_entries),
        "min_funnel_score": float(getattr(inputs.cfg, "min_funnel_score", 0.0) or 0.0),
        "markup_symbols": candidates.markup_symbols,
        "accum_stage_map": candidates.accum_stage_map,
        "exit_signals": candidates.exit_signals,
    }


def _bypass_metrics(inputs: FunnelMetricsInputs) -> dict:
    return {
        "l2_bypass_pool": inputs.l2_bypass_pool,
        "l2_bypass_triggers": inputs.l2_bypass_triggers,
        "strategic_l2_bypass_seed_count": len(inputs.strategic.seed_codes),
        "strategic_l2_bypass_pool": inputs.strategic.pool,
        "strategic_l2_bypass_triggers": inputs.strategic.triggers,
        "strategic_l2_bypass_stage_map": inputs.strategic.stage_map,
        "strategic_l2_bypass_rescue_map": inputs.strategic.rescue_map,
        "strategic_l2_bypass_markup_symbols": inputs.strategic.markup_symbols,
        "strategic_l2_bypass_reason_map": inputs.strategic.reason_map,
    }


def _tail_context_metrics(inputs: FunnelMetricsInputs) -> dict:
    return {
        "benchmark_context": inputs.benchmark_context,
        "latest_close_map": _latest_close_map(inputs.all_df_map),
        "all_df_map": inputs.all_df_map,
        "financial_map": inputs.financial_map,
    }


def _attach_funnel_debug_context(metrics: dict, inputs: FunnelMetricsInputs, include_debug_context: bool) -> None:
    if not include_debug_context:
        return
    metrics["_debug"] = {
        "cfg": inputs.cfg,
        "end_trade_date": inputs.window.end_trade_date.isoformat(),
        "all_symbols": inputs.pool.symbols,
        "name_map": inputs.ref_data.name_map,
        "market_cap_map": inputs.ref_data.market_cap_map,
        "sector_map": inputs.ref_data.sector_map,
        "bench_df": inputs.bench_df,
        "all_df_map": inputs.all_df_map,
        "layer1_symbols": inputs.layers.l1_passed,
        "layer2_symbols": inputs.layers.l2_passed,
        "layer3_symbols_raw": inputs.layers.l3_passed,
    }


def _log_funnel_summary(metrics: dict, inputs: FunnelMetricsInputs) -> None:
    counts = inputs.layers.l2_counts
    print(
        f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
        f"(主升={counts['momentum']}, 潜伏={counts['ambush']}, 吸筹={counts['accum']}, "
        f"地量={counts['dry_vol']}, 护盘={counts['rs_div']}, 趋势={counts['trend_cont']}, 点火={counts['sos']}), "
        f"L3={metrics['layer3']}, 命中={inputs.candidates.total_hits}, "
        f"Top板块={inputs.layers.top_sectors}, 热门概念={inputs.ref_data.hot_concepts[:3] if inputs.ref_data.hot_concepts else []}, "
        f"战略旁路={len(inputs.strategic.pool)}, 主线候选分布={_mainline_log_counts(inputs.layers.mainline_candidates)}, "
        f"Alpha候选={len(inputs.candidates.candidate_entries)}, "
        f"趋势观察={len(inputs.layers.leader_radar_rows)}, 各触发={metrics['by_trigger']}"
    )
    print(f"[funnel] 主题雷达({inputs.layers.theme_radar_source}): {summarize_theme_radar(inputs.layers.theme_radar)}")
    _report_progress("筛选完成", f"命中={inputs.candidates.total_hits}只", 1.0)


def _build_run_artifacts(data) -> FunnelRunArtifacts:
    layers = run_base_funnel_layers(
        all_df_map=data.all_df_map,
        bench_df=data.bench_df,
        window=data.window,
        cfg=data.cfg,
        ref_data=data.ref_data,
        etf_l2_passed=data.etf_l2_passed,
        etf_sector_map=data.etf_sector_map,
        etf_df_map=data.etf_df_map,
        benchmark_context=data.benchmark_context,
    )
    l2_bypass_pool, bypass_triggers = _build_l2_bypass(data, layers)
    external_seed_review = _review_external_seed(data, layers)
    _log_external_seed_review(data.pool.external_seed_cfg, external_seed_review)
    strategic = build_strategic_bypass_from_theme(
        layers=layers,
        all_df_map=data.all_df_map,
        cfg=data.cfg,
        market_cap_map=data.ref_data.market_cap_map,
    )
    candidates = build_candidate_outputs(
        layers=layers,
        strategic=strategic,
        all_df_map=data.all_df_map,
        sector_map=data.ref_data.sector_map,
        cfg=data.cfg,
    )
    return FunnelRunArtifacts(layers, l2_bypass_pool, bypass_triggers, strategic, candidates, external_seed_review)


def _mainline_log_counts(candidates: list[dict]) -> str:
    counts = _mainline_status_counts(candidates)
    return (
        f"买点{counts['主线买点候选']}/分歧{counts['强主线分歧']}/"
        f"修复{counts['事件主题修复候选']}/观察{counts['主线观察']}/鱼尾{counts['过热不追']}"
    )


def _mainline_status_counts(candidates: list[dict]) -> dict[str, int]:
    counts = {"主线买点候选": 0, "强主线分歧": 0, "事件主题修复候选": 0, "主线观察": 0, "过热不追": 0}
    for item in candidates or []:
        status = str(item.get("status") or "主线观察")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _build_l2_bypass(data, layers: FunnelLayerOutputs) -> tuple[list[str], dict[str, list[tuple[str, float]]]]:
    return build_l2_bypass_pool(
        l1_passed=layers.l1_passed,
        l2_passed=layers.l2_passed,
        top_sectors=layers.top_sectors,
        sector_map=data.ref_data.sector_map,
        all_df_map=data.all_df_map,
        cfg=data.cfg,
        channel_map=layers.l2_channel_map,
        market_cap_map=data.ref_data.market_cap_map,
    )


def _review_external_seed(data, layers: FunnelLayerOutputs) -> dict:
    return _build_external_seed_review(
        data.pool.external_seed_cfg,
        data.window.end_trade_date.isoformat(),
        layers.l1_passed,
        layers.l2_passed,
        data.all_df_map,
        data.cfg,
        layers.l2_channel_map,
        data.ref_data.market_cap_map,
        data.ref_data.name_map,
        data.ref_data.sector_map,
    )


def run_funnel_job(
    include_debug_context: bool = False,
    direct_source: bool = False,
    pool_board: str | None = None,
    pool_limit_count: int | None = None,
    executor_mode: str | None = None,
    include_financial_metrics: bool = True,
) -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    data = prepare_funnel_job_data(
        direct_source,
        enforce_target_trade_date=ENFORCE_TARGET_TRADE_DATE,
        pool_board=pool_board,
        pool_limit_count=pool_limit_count,
        executor_mode=executor_mode,
        include_financial_metrics=include_financial_metrics,
    )
    artifacts = _build_run_artifacts(data)
    metrics_inputs = FunnelMetricsInputs(
        cfg=data.cfg,
        pool=data.pool,
        window=data.window,
        fetch_stats=data.fetch_stats,
        snapshot_dir=data.snapshot_dir,
        layers=artifacts.layers,
        ref_data=data.ref_data,
        bench_df=data.bench_df,
        etf_symbols=data.etf_symbols,
        etf_sector_map=data.etf_sector_map,
        etf_df_map=data.etf_df_map,
        etf_l2_passed=data.etf_l2_passed,
        etf_candidates=data.etf_candidates,
        l2_bypass_pool=artifacts.l2_bypass_pool,
        l2_bypass_triggers=artifacts.l2_bypass_triggers,
        strategic=artifacts.strategic,
        candidates=artifacts.candidates,
        external_seed_cfg=data.pool.external_seed_cfg,
        external_added_to_pool=data.pool.external_added_to_pool,
        external_seed_review=artifacts.external_seed_review,
        benchmark_context=data.benchmark_context,
        all_df_map=data.all_df_map,
        financial_map=data.ref_data.financial_map,
        financial_metrics_requested=include_financial_metrics,
    )
    metrics = _build_funnel_metrics(metrics_inputs)
    _attach_funnel_debug_context(metrics, metrics_inputs, include_debug_context)
    _log_funnel_summary(metrics, metrics_inputs)
    return artifacts.layers.triggers, metrics


def run(
    webhook_url: str,
    *,
    notify: bool = True,
    return_details: bool = False,
    pool_board: str | None = None,
    pool_limit_count: int | None = None,
    executor_mode: str | None = None,
    include_financial_metrics: bool = True,
) -> tuple[bool, list[dict], dict] | tuple[bool, list[dict], dict, dict]:
    """
    执行 Wyckoff Funnel，漏斗完成后立即发送飞书通知。
    返回 (成功与否, 用于研报的股票信息列表, 大盘上下文)。
    每项为 {"code": str, "name": str, "tag": str}。
    """
    triggers, metrics = run_funnel_job(
        pool_board=pool_board,
        pool_limit_count=pool_limit_count,
        executor_mode=executor_mode,
        include_financial_metrics=include_financial_metrics,
    )
    render_ctx = build_render_context(triggers, metrics)
    full_formal, legacy_selection, legacy_card = _selection_mode_flags()
    l3_ranked_symbols = [str(c).strip() for c in (metrics.get("layer3_symbols", []) or []) if str(c).strip()]
    ai_selection = _select_run_ai_candidates(render_ctx, l3_ranked_symbols, full_formal or legacy_selection)
    ai_selection = _apply_data_quality_mode(ai_selection, metrics)
    return deliver_funnel_selection(
        render_ctx,
        ai_selection,
        legacy_card=legacy_card and legacy_selection,
        webhook_url=webhook_url,
        notify=notify,
        return_details=return_details,
    )
