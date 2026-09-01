"""Build render context objects for funnel notification output."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from core.candidate_policy import candidate_score_value
from core.candidate_ranker import TRIGGER_LABELS
from core.candidate_tracks import best_candidate_entry_map, candidate_entry_key
from core.funnel_report import FunnelReportMaps
from core.funnel_selection import merge_trigger_maps, rank_l2_bypass_pool
from core.funnel_theme import append_theme_reasons, apply_theme_bonus_to_scores
from core.funnel_theme import capital_migration_badge_map as build_capital_migration_badge_map
from core.funnel_theme import capital_migration_bonus_map as build_capital_migration_bonus_map
from core.funnel_theme import theme_badge_map as build_theme_badge_map
from core.funnel_theme import theme_bonus_map as build_theme_bonus_map
from core.funnel_theme import theme_candidate_map as build_theme_candidate_map
from core.mainline_engine import TRADEABLE_MAINLINE_STATUSES
from integrations.market_metadata import fetch_sector_map
from tools.symbol_pool import load_stock_name_map
from workflows.funnel_settings import (
    FUNNEL_CAPITAL_MIGRATION_BONUS_MAX,
    FUNNEL_CAPITAL_MIGRATION_PENALTY_MAX,
    FUNNEL_THEME_RADAR_BONUS_MAX,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FunnelRenderContext:
    metrics: dict
    formal_triggers: dict[str, list[tuple[str, float]]]
    review_triggers: dict[str, list[tuple[str, float]]]
    benchmark_context: dict
    all_df_map: dict[str, pd.DataFrame]
    name_map: dict[str, str]
    sector_map: dict[str, str]
    latest_close_map: dict[str, float]
    theme_candidate_map: dict
    theme_badge_map: dict[str, str]
    theme_bonus_map: dict[str, float]
    capital_migration_bonus_map: dict[str, float]
    l2_bypass_pool: list[str]
    bypass_triggers: dict[str, list[tuple[str, float]]]
    strategic_l2_bypass_pool: list[str]
    strategic_l2_bypass_triggers: dict[str, list[tuple[str, float]]]
    strategic_l2_bypass_stage_map: dict[str, str]
    leader_radar_rows: list[dict]
    leader_radar_symbols: set[str]
    candidate_entries: list[dict]
    candidate_entry_map: dict[str, dict]
    mainline_candidates: list[dict]
    mainline_tradeable: list[dict]
    mainline_observe: list[dict]
    mainline_overheated: list[dict]
    mainline_tradeable_codes: list[str]
    mainline_candidate_set: set[str]
    external_seed_triggers: dict[str, list[tuple[str, float]]]
    formal_hit_set: set[str]
    l2_bypass_set: set[str]
    strategic_l2_bypass_set: set[str]
    code_to_reasons: dict[str, list[str]]
    code_to_trigger_keys: dict[str, list[str]]
    code_to_total_score: dict[str, float]
    formal_sorted_codes: list[str]
    unique_hit_count: int
    review_unique_count: int
    l2_bypass_ranked: list[str]
    strategic_l2_bypass_ranked: list[str]
    l2_channel_map: dict[str, str]
    markup_symbols: list[str]
    accum_stage_map: dict[str, str]
    exit_signals: dict[str, dict]
    sector_rotation_map: dict[str, dict]
    report_maps: FunnelReportMaps
    theme_l4_count: int
    theme_radar_source: str
    external_seed_line: str
    regime: str


@dataclass(frozen=True)
class _RenderContextParts:
    metrics: dict
    triggers: dict[str, list[tuple[str, float]]]
    benchmark_context: dict
    name_map: dict[str, str]
    sector_map: dict[str, str]
    latest_close_map: dict[str, float]
    theme_candidate_map: dict
    theme_badge_map: dict[str, str]
    theme_bonus_map: dict[str, float]
    capital_migration_bonus_map: dict[str, float]
    l2_bypass_pool: list[str]
    strategic_pool: list[str]
    bypass_triggers: dict[str, list[tuple[str, float]]]
    strategic_triggers: dict[str, list[tuple[str, float]]]
    candidate_entries: list[dict]
    candidate_entry_map: dict[str, dict]
    review_triggers: dict[str, list[tuple[str, float]]]
    formal_hit_set: set[str]
    strategic_set: set[str]
    code_to_reasons: dict[str, list[str]]
    code_to_trigger_keys: dict[str, list[str]]
    code_to_total_score: dict[str, float]
    sorted_codes: list[str]
    leader_rows: list[dict]
    mainline_candidates: list[dict]
    exit_signals: dict[str, dict]
    sector_rotation_map: dict[str, dict]
    report_maps: FunnelReportMaps


@dataclass(frozen=True)
class _ReviewScoreContext:
    review_triggers: dict[str, list[tuple[str, float]]]
    formal_hit_set: set[str]
    strategic_set: set[str]
    code_to_reasons: dict[str, list[str]]
    code_to_trigger_keys: dict[str, list[str]]
    code_to_total_score: dict[str, float]
    sorted_codes: list[str]


@dataclass(frozen=True)
class _ThemeMaps:
    theme_candidate_map: dict
    theme_badge_map: dict[str, str]
    theme_bonus_map: dict[str, float]
    capital_migration_bonus_map: dict[str, float]
    capital_migration_badge_map: dict[str, str]


@dataclass(frozen=True)
class _MetricPools:
    l2_bypass_pool: list[str]
    strategic_pool: list[str]
    bypass_triggers: dict[str, list[tuple[str, float]]]
    strategic_triggers: dict[str, list[tuple[str, float]]]
    candidate_entries: list[dict]
    candidate_entry_map: dict[str, dict]
    mainline_candidates: list[dict]
    leader_rows: list[dict]
    exit_signals: dict[str, dict]
    sector_rotation_map: dict[str, dict]


def build_render_context(triggers: dict[str, list[tuple[str, float]]], metrics: dict) -> FunnelRenderContext:
    return _render_context_from_parts(_build_render_context_parts(triggers, metrics))


def _build_render_context_parts(triggers: dict[str, list[tuple[str, float]]], metrics: dict) -> _RenderContextParts:
    (
        benchmark_context,
        name_map,
        sector_map,
        latest_close_map,
        theme_candidate_map,
        theme_badge_map,
        theme_bonus_map,
        capital_migration_bonus_map,
        capital_migration_badge_map,
    ) = _base_render_context(metrics)
    pools = _metric_pools(metrics)
    score_ctx = _build_review_score_context(
        metrics,
        triggers,
        pools.strategic_pool,
        pools.bypass_triggers,
        pools.strategic_triggers,
        pools.candidate_entry_map,
        theme_badge_map,
        theme_bonus_map,
        capital_migration_badge_map,
        capital_migration_bonus_map,
        benchmark_context=benchmark_context,
    )
    report_maps = _build_report_maps(
        name_map,
        sector_map,
        pools.sector_rotation_map,
        pools.exit_signals,
        latest_close_map,
        theme_candidate_map,
        theme_bonus_map,
        score_ctx,
        theme_badge_map,
        capital_migration_bonus_map,
        metrics.get("layer3_score_map", {}) or {},
    )
    return _RenderContextParts(
        metrics=metrics,
        triggers=triggers,
        benchmark_context=benchmark_context,
        name_map=name_map,
        sector_map=sector_map,
        latest_close_map=latest_close_map,
        theme_candidate_map=theme_candidate_map,
        theme_badge_map=theme_badge_map,
        theme_bonus_map=theme_bonus_map,
        capital_migration_bonus_map=capital_migration_bonus_map,
        l2_bypass_pool=pools.l2_bypass_pool,
        strategic_pool=pools.strategic_pool,
        bypass_triggers=pools.bypass_triggers,
        strategic_triggers=pools.strategic_triggers,
        candidate_entries=pools.candidate_entries,
        candidate_entry_map=pools.candidate_entry_map,
        review_triggers=score_ctx.review_triggers,
        formal_hit_set=score_ctx.formal_hit_set,
        strategic_set=score_ctx.strategic_set,
        code_to_reasons=score_ctx.code_to_reasons,
        code_to_trigger_keys=score_ctx.code_to_trigger_keys,
        code_to_total_score=score_ctx.code_to_total_score,
        sorted_codes=score_ctx.sorted_codes,
        leader_rows=pools.leader_rows,
        mainline_candidates=pools.mainline_candidates,
        exit_signals=pools.exit_signals,
        sector_rotation_map=pools.sector_rotation_map,
        report_maps=report_maps,
    )


def _metric_pools(metrics: dict) -> _MetricPools:
    candidate_entries = metrics.get("candidate_entries", []) or []
    return _MetricPools(
        l2_bypass_pool=metrics.get("l2_bypass_pool", []) or [],
        strategic_pool=metrics.get("strategic_l2_bypass_pool", []) or [],
        bypass_triggers=metrics.get("l2_bypass_triggers", {}) or {},
        strategic_triggers=metrics.get("strategic_l2_bypass_triggers", {}) or {},
        candidate_entries=candidate_entries,
        candidate_entry_map=_entry_map(candidate_entries),
        mainline_candidates=metrics.get("mainline_candidates", []) or [],
        leader_rows=metrics.get("leader_radar_rows", []) or [],
        exit_signals=metrics.get("exit_signals", {}) or {},
        sector_rotation_map=(metrics.get("sector_rotation", {}) or {}).get("state_map", {}) or {},
    )


def _build_report_maps(
    name_map: dict[str, str],
    sector_map: dict[str, str],
    sector_rotation_map: dict[str, dict],
    exit_signals: dict[str, dict],
    latest_close_map: dict[str, float],
    theme_candidate_map: dict,
    theme_bonus_map: dict[str, float],
    score_ctx: _ReviewScoreContext,
    theme_badge_map: dict[str, str],
    capital_migration_bonus_map: dict[str, float],
    layer3_score_map: dict[str, float],
) -> FunnelReportMaps:
    return FunnelReportMaps(
        name_map,
        sector_map,
        sector_rotation_map,
        exit_signals,
        latest_close_map,
        theme_candidate_map,
        theme_bonus_map,
        score_ctx.code_to_trigger_keys,
        score_ctx.code_to_reasons,
        theme_badge_map,
        capital_migration_bonus_map,
        layer3_score_map,
    )


def rebuild_report_maps(
    metrics: dict,
    *,
    name_map: dict[str, str] | None = None,
    sector_map: dict[str, str] | None = None,
    triggers: dict[str, list[tuple[str, float]]] | None = None,
) -> FunnelReportMaps:
    """从 metrics 复原报告字段族映射,不触发任何网络 I/O。

    `build_render_context` 只在渲染期存在,`FunnelReportMaps` 不进 step2_details,
    所以绕过 `build_symbol_report_row` 的写入方(起跳板/跨日确认)拿不到行业轮动、
    主题、资金迁移、离场信号字段。这里用同一套纯函数复原,name_map/sector_map 由
    调用方从 step2_details 传入,避免重复 `load_stock_name_map()`/`fetch_sector_map()`。
    """
    themes = _theme_maps(metrics)
    pools = _metric_pools(metrics)
    score_ctx = _build_review_score_context(
        metrics,
        triggers or {},
        pools.strategic_pool,
        pools.bypass_triggers,
        pools.strategic_triggers,
        pools.candidate_entry_map,
        themes.theme_badge_map,
        themes.theme_bonus_map,
        themes.capital_migration_badge_map,
        themes.capital_migration_bonus_map,
        benchmark_context=metrics.get("benchmark_context", {}) or {},
    )
    return _build_report_maps(
        name_map or {},
        sector_map or {},
        pools.sector_rotation_map,
        pools.exit_signals,
        metrics.get("latest_close_map", {}) or {},
        themes.theme_candidate_map,
        themes.theme_bonus_map,
        score_ctx,
        themes.theme_badge_map,
        themes.capital_migration_bonus_map,
        metrics.get("layer3_score_map", {}) or {},
    )


def _build_review_score_context(
    metrics: dict,
    triggers: dict[str, list[tuple[str, float]]],
    strategic_pool: list[str],
    bypass_triggers: dict[str, list[tuple[str, float]]],
    strategic_triggers: dict[str, list[tuple[str, float]]],
    candidate_entry_map: dict[str, dict],
    theme_badge_map: dict[str, str],
    theme_bonus_map: dict[str, float],
    capital_migration_badge_map: dict[str, str],
    capital_migration_bonus_map: dict[str, float],
    benchmark_context: dict | None = None,
) -> _ReviewScoreContext:
    review_triggers = merge_trigger_maps(triggers, bypass_triggers, strategic_triggers)
    formal_hit_set = {str(code).strip() for hits in triggers.values() for code, _ in hits if str(code).strip()}
    strategic_set = {str(c).strip() for c in strategic_pool if str(c).strip()}
    code_to_reasons, code_to_trigger_keys, code_to_total_score = _build_review_score_maps(
        review_triggers=review_triggers,
        candidate_entry_map=candidate_entry_map,
        strategic_l2_bypass_set=strategic_set,
        strategic_l2_bypass_reason_map=metrics.get("strategic_l2_bypass_reason_map", {}) or {},
        theme_badge_map=theme_badge_map,
        theme_bonus_map=theme_bonus_map,
        capital_migration_badge_map=capital_migration_badge_map,
        capital_migration_bonus_map=capital_migration_bonus_map,
        benchmark_context=benchmark_context,
    )
    sorted_codes = sorted(code_to_reasons.keys(), key=lambda c: -candidate_score_value(code_to_total_score.get(c)))
    return _ReviewScoreContext(
        review_triggers,
        formal_hit_set,
        strategic_set,
        code_to_reasons,
        code_to_trigger_keys,
        code_to_total_score,
        sorted_codes,
    )


def _base_render_context(
    metrics: dict,
) -> tuple[
    dict,
    dict[str, str],
    dict[str, str],
    dict[str, float],
    dict,
    dict[str, str],
    dict[str, float],
    dict[str, float],
    dict[str, str],
]:
    benchmark_context = metrics.get("benchmark_context", {}) or {}
    name_map, sector_map, latest_close_map = _load_run_reference_maps(metrics, benchmark_context)
    themes = _theme_maps(metrics)
    return (
        benchmark_context,
        name_map,
        sector_map,
        latest_close_map,
        themes.theme_candidate_map,
        themes.theme_badge_map,
        themes.theme_bonus_map,
        themes.capital_migration_bonus_map,
        themes.capital_migration_badge_map,
    )


def _theme_maps(metrics: dict) -> _ThemeMaps:
    """主题/资金迁移映射,纯函数(只读 metrics),离线复原报告字段族时可复用。"""
    theme_candidate_map = build_theme_candidate_map(metrics.get("theme_radar") or {})
    theme_bonus_map = build_theme_bonus_map(theme_candidate_map, FUNNEL_THEME_RADAR_BONUS_MAX)
    capital_migration_bonus_map = build_capital_migration_bonus_map(
        theme_candidate_map,
        metrics.get("capital_migration") or {},
        bonus_max=FUNNEL_CAPITAL_MIGRATION_BONUS_MAX,
        penalty_max=FUNNEL_CAPITAL_MIGRATION_PENALTY_MAX,
    )
    return _ThemeMaps(
        theme_candidate_map=theme_candidate_map,
        theme_badge_map=build_theme_badge_map(theme_candidate_map),
        theme_bonus_map=theme_bonus_map,
        capital_migration_bonus_map=capital_migration_bonus_map,
        capital_migration_badge_map=build_capital_migration_badge_map(
            theme_candidate_map,
            capital_migration_bonus_map,
        ),
    )


def _load_run_reference_maps(
    metrics: dict, benchmark_context: dict
) -> tuple[dict[str, str], dict[str, str], dict[str, float]]:
    try:
        name_map = load_stock_name_map()
    except Exception as exc:
        logger.warning("股票名称加载失败，降级为代码展示: %s", exc)
        name_map = {}
    try:
        sector_map = fetch_sector_map()
    except Exception as exc:
        logger.warning("行业映射加载失败，降级为空映射: %s", exc)
        sector_map = {}
    latest_close_map = metrics.get("latest_close_map", {}) or {}
    if latest_close_map:
        benchmark_context["latest_close_map"] = latest_close_map
    return name_map, sector_map, latest_close_map


def _entry_map(candidate_entries: list[dict]) -> dict[str, dict]:
    return best_candidate_entry_map(candidate_entries)


def _build_review_score_maps(
    *,
    review_triggers: dict[str, list[tuple[str, float]]],
    candidate_entry_map: dict[str, dict],
    strategic_l2_bypass_set: set[str],
    strategic_l2_bypass_reason_map: dict[str, list[str]],
    theme_badge_map: dict[str, str],
    theme_bonus_map: dict[str, float],
    capital_migration_badge_map: dict[str, str],
    capital_migration_bonus_map: dict[str, float],
    benchmark_context: dict | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, float]]:
    code_to_reasons: dict[str, list[str]] = {}
    code_to_trigger_keys: dict[str, list[str]] = {}
    code_to_total_score: dict[str, float] = {}
    _add_trigger_reasons(review_triggers, code_to_reasons, code_to_trigger_keys, code_to_total_score)
    _add_candidate_entry_reasons(candidate_entry_map, code_to_reasons, code_to_trigger_keys, code_to_total_score)
    for code in strategic_l2_bypass_set:
        code_to_reasons.setdefault(code, [])
        code_to_trigger_keys.setdefault(code, [])
        code_to_total_score.setdefault(code, 0.0)
    _append_extra_reasons(code_to_reasons, strategic_l2_bypass_reason_map)
    append_theme_reasons(code_to_reasons, theme_badge_map)
    append_theme_reasons(code_to_reasons, capital_migration_badge_map)
    apply_theme_bonus_to_scores(code_to_total_score, theme_bonus_map)
    apply_theme_bonus_to_scores(code_to_total_score, capital_migration_bonus_map)

    if benchmark_context and "l3_passed_normal" in benchmark_context:
        l3_passed_normal = set(benchmark_context["l3_passed_normal"])
        for code in code_to_total_score:
            if code in l3_passed_normal:
                code_to_total_score[code] = candidate_score_value(code_to_total_score.get(code)) + 8.0
                code_to_reasons.setdefault(code, []).append("板块共振加分(+8.0)")

    return code_to_reasons, code_to_trigger_keys, code_to_total_score


def _add_trigger_reasons(
    review_triggers: dict[str, list[tuple[str, float]]],
    code_to_reasons: dict[str, list[str]],
    code_to_trigger_keys: dict[str, list[str]],
    code_to_total_score: dict[str, float],
) -> None:
    for key, label in TRIGGER_LABELS.items():
        for code, score in review_triggers.get(key, []):
            code_to_reasons.setdefault(code, []).append(label)
            code_to_trigger_keys.setdefault(code, []).append(key)
            code_to_total_score[code] = candidate_score_value(code_to_total_score.get(code)) + candidate_score_value(
                score
            )


def _add_candidate_entry_reasons(
    candidate_entry_map: dict[str, dict],
    code_to_reasons: dict[str, list[str]],
    code_to_trigger_keys: dict[str, list[str]],
    code_to_total_score: dict[str, float],
) -> None:
    for code, item in candidate_entry_map.items():
        signal_key = candidate_entry_key(item, fields=("signal_key", "lane", "entry_type")) or "alpha_candidate"
        entry_type = str(item.get("entry_type") or signal_key)
        reasons = [str(x).strip() for x in item.get("reasons", []) if str(x).strip()]
        reason_text = f"{entry_type}: " + " / ".join(reasons[:3]) if reasons else entry_type
        code_to_reasons.setdefault(code, [])
        code_to_trigger_keys.setdefault(code, [])
        code_to_total_score[code] = max(
            candidate_score_value(code_to_total_score.get(code)),
            candidate_score_value(item.get("score")),
        )
        if reason_text not in code_to_reasons[code]:
            code_to_reasons[code].append(reason_text)
        if signal_key not in code_to_trigger_keys[code]:
            code_to_trigger_keys[code].append(signal_key)


def _append_extra_reasons(code_to_reasons: dict[str, list[str]], reason_map: dict[str, list[str]]) -> None:
    for code, reasons in reason_map.items():
        bucket = code_to_reasons.setdefault(code, [])
        for reason in reasons:
            if reason and reason not in bucket:
                bucket.append(reason)


def _external_seed_report_line(metrics: dict) -> str:
    seed_count = int(metrics.get("external_seed_count") or 0)
    if seed_count <= 0:
        return ""
    source = str(metrics.get("external_seed_source") or "external")
    confirmed = len(metrics.get("external_seed_l4_confirmed_codes") or [])
    watch = len(metrics.get("external_seed_watch_codes") or [])
    rejected = len(metrics.get("external_seed_rejected_l1_codes") or [])
    ttl = int(metrics.get("external_seed_watch_ttl_days") or 0)
    return f"{source}: seeds={seed_count}, L4确认={confirmed}, WATCH={watch}, L1拒绝={rejected}, ttl={ttl}d"


def _render_context_from_parts(parts: _RenderContextParts) -> FunnelRenderContext:
    return FunnelRenderContext(
        metrics=parts.metrics,
        formal_triggers=parts.triggers,
        review_triggers=parts.review_triggers,
        benchmark_context=parts.benchmark_context,
        all_df_map=parts.metrics.get("all_df_map", {}) or {},
        name_map=parts.name_map,
        sector_map=parts.sector_map,
        latest_close_map=parts.latest_close_map,
        theme_candidate_map=parts.theme_candidate_map,
        theme_badge_map=parts.theme_badge_map,
        theme_bonus_map=parts.theme_bonus_map,
        capital_migration_bonus_map=parts.capital_migration_bonus_map,
        l2_bypass_pool=parts.l2_bypass_pool,
        bypass_triggers=parts.bypass_triggers,
        strategic_l2_bypass_pool=parts.strategic_pool,
        strategic_l2_bypass_triggers=parts.strategic_triggers,
        strategic_l2_bypass_stage_map=parts.metrics.get("strategic_l2_bypass_stage_map", {}) or {},
        leader_radar_rows=parts.leader_rows,
        leader_radar_symbols={str(row.get("code", "")).strip() for row in parts.leader_rows if row.get("code")},
        candidate_entries=parts.candidate_entries,
        candidate_entry_map=parts.candidate_entry_map,
        mainline_candidates=parts.mainline_candidates,
        mainline_tradeable=_mainline_by_status(parts.mainline_candidates, TRADEABLE_MAINLINE_STATUSES),
        mainline_observe=_mainline_by_status(parts.mainline_candidates, "主线观察"),
        mainline_overheated=_mainline_by_status(parts.mainline_candidates, "过热不追"),
        mainline_tradeable_codes=_mainline_codes_by_status(parts.mainline_candidates, TRADEABLE_MAINLINE_STATUSES),
        mainline_candidate_set={
            str(row.get("code", "")).strip() for row in parts.mainline_candidates if row.get("code")
        },
        external_seed_triggers=parts.metrics.get("external_seed_l4_triggers", {}) or {},
        formal_hit_set=parts.formal_hit_set,
        l2_bypass_set=set(parts.l2_bypass_pool),
        strategic_l2_bypass_set=parts.strategic_set,
        code_to_reasons=parts.code_to_reasons,
        code_to_trigger_keys=parts.code_to_trigger_keys,
        code_to_total_score=parts.code_to_total_score,
        formal_sorted_codes=[code for code in parts.sorted_codes if code in parts.formal_hit_set],
        unique_hit_count=len(parts.formal_hit_set),
        review_unique_count=len(parts.sorted_codes),
        l2_bypass_ranked=rank_l2_bypass_pool(parts.l2_bypass_pool, parts.code_to_total_score),
        strategic_l2_bypass_ranked=rank_l2_bypass_pool(parts.strategic_pool, parts.code_to_total_score),
        l2_channel_map=parts.metrics.get("layer2_channel_map", {}) or {},
        markup_symbols=parts.metrics.get("markup_symbols", []) or [],
        accum_stage_map=parts.metrics.get("accum_stage_map", {}) or {},
        exit_signals=parts.exit_signals,
        sector_rotation_map=parts.sector_rotation_map,
        report_maps=parts.report_maps,
        theme_l4_count=sum(1 for c in parts.formal_hit_set if c in parts.theme_candidate_map),
        theme_radar_source=str(parts.metrics.get("theme_radar_source") or "current"),
        external_seed_line=_external_seed_report_line(parts.metrics),
        regime=str(parts.benchmark_context.get("regime", "NEUTRAL")),
    )


def _mainline_by_status(rows: list[dict], status: str | set[str]) -> list[dict]:
    statuses = {status} if isinstance(status, str) else status
    return [row for row in rows if str(row.get("status") or "") in statuses]


def _mainline_codes_by_status(rows: list[dict], status: str | set[str]) -> list[str]:
    return [str(row.get("code")).strip() for row in _mainline_by_status(rows, status) if str(row.get("code")).strip()]
