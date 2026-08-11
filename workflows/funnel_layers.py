"""Layer execution workflow for the A-share funnel job."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from core.funnel_theme import empty_theme_snapshot, select_linked_theme_radar
from core.funnel_theme import theme_candidate_map as build_theme_candidate_map
from core.mainline_engine import build_mainline_candidates
from core.sector_rotation import analyze_sector_rotation
from core.theme_activity import build_theme_activity_snapshot
from core.theme_radar import build_theme_radar_snapshot
from core.wyckoff_engine import (
    FunnelConfig,
    detect_leader_radar,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer4_triggers,
)
from core.wyckoff_structure import build_structure_shadow, detect_structure_triggers
from integrations.market_metadata import CONCEPT_HEAT_HISTORY
from integrations.ths_hot_concept import merge_concept_heat
from tools.mainline_config import load_mainline_engine_config
from utils.progress import report_progress as _report_progress
from utils.safe import safe_float as _safe_float
from workflows.funnel_data import FunnelReferenceData
from workflows.funnel_settings import (
    FUNNEL_THEME_RADAR_ENABLED,
    FUNNEL_THEME_RADAR_LINK_ENABLED,
    FUNNEL_THEME_RADAR_MAX_AGE_DAYS,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FunnelLayerOutputs:
    l1_passed: list[str]
    l2_passed: list[str]
    l2_channel_map: dict[str, str]
    l2_rejections: dict[str, str]
    l2_counts: dict[str, int]
    l3_passed: list[str]
    top_sectors: list[str]
    sector_rotation: dict
    triggers: dict[str, list[tuple[str, float]]]
    structure_shadow: dict
    leader_radar_rows: list[dict]
    leader_radar_symbols: list[str]
    theme_radar_current: dict
    theme_radar: dict
    theme_radar_source: str
    theme_activity: dict
    theme_candidate_map: dict
    mainline_candidates: list[dict]
    mainline_ai_cap: int
    rps_universe_count: int


def run_base_funnel_layers(
    *,
    all_df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    window,
    cfg: FunnelConfig,
    ref_data: FunnelReferenceData,
    etf_l2_passed: list[str],
    etf_sector_map: dict[str, str],
    etf_df_map: dict[str, pd.DataFrame],
    benchmark_context: dict,
) -> FunnelLayerOutputs:
    print("[funnel] 开始执行全量漏斗筛选...")
    _report_progress("漏斗筛选", "L1~L4 计算中", 0.85)
    l1_input = list(all_df_map.keys())
    l1_passed, l2_passed, l2_channel_map, l2_rejections = _run_strength_layers(
        l1_input, all_df_map, bench_df, cfg, ref_data
    )
    theme_activity = _build_theme_activity(window, ref_data, all_df_map)
    l3_passed, top_sectors, sector_rotation = _run_sector_layer(
        l1_passed,
        l2_passed,
        all_df_map,
        cfg,
        ref_data,
        etf_l2_passed,
        etf_sector_map,
        etf_df_map,
        _hot_concepts(ref_data, theme_activity),
        regime=benchmark_context.get("regime"),
        benchmark_context=benchmark_context,
    )
    benchmark_context["sector_rotation"] = sector_rotation
    triggers = layer4_triggers(
        l3_passed, all_df_map, cfg, channel_map=l2_channel_map, market_cap_map=ref_data.market_cap_map
    )
    structure_shadow = _structure_shadow(l3_passed, all_df_map, cfg, triggers)
    leader_rows = detect_leader_radar(l1_passed, all_df_map, ref_data.sector_map, l2_channel_map, cfg)
    theme_current, theme_radar, theme_source = _build_theme_context(window, ref_data, all_df_map)
    mainline_cfg = load_mainline_engine_config()
    mainline_candidates = _build_mainline_candidates_helper(
        l1_passed=l1_passed,
        l2_passed=l2_passed,
        ref_data=ref_data,
        theme_current=theme_current,
        theme_activity=theme_activity,
        mainline_cfg=mainline_cfg,
        all_df_map=all_df_map,
    )
    return _build_funnel_layer_outputs(
        l1_input=l1_input,
        l1_passed=l1_passed,
        l2_passed=l2_passed,
        l2_channel_map=l2_channel_map,
        l2_rejections=l2_rejections,
        l3_passed=l3_passed,
        top_sectors=top_sectors,
        sector_rotation=sector_rotation,
        triggers=triggers,
        structure_shadow=structure_shadow,
        leader_rows=leader_rows,
        theme_current=theme_current,
        theme_radar=theme_radar,
        theme_source=theme_source,
        theme_activity=theme_activity,
        mainline_candidates=mainline_candidates,
        mainline_cfg=mainline_cfg,
    )


def _build_funnel_layer_outputs(
    l1_input: list[str],
    l1_passed: list[str],
    l2_passed: list[str],
    l2_channel_map: dict[str, str],
    l2_rejections: dict[str, str],
    l3_passed: list[str],
    top_sectors: list[str],
    sector_rotation: dict,
    triggers: dict,
    structure_shadow: dict,
    leader_rows: list[dict],
    theme_current: dict,
    theme_radar: dict,
    theme_source: str,
    theme_activity: dict,
    mainline_candidates: list[dict],
    mainline_cfg: Any,
) -> FunnelLayerOutputs:
    return FunnelLayerOutputs(
        l1_passed=l1_passed,
        l2_passed=l2_passed,
        l2_channel_map=l2_channel_map,
        l2_rejections=l2_rejections,
        l2_counts=_l2_channel_counts(l2_channel_map),
        l3_passed=l3_passed,
        top_sectors=top_sectors,
        sector_rotation=sector_rotation,
        triggers=triggers,
        structure_shadow=structure_shadow,
        leader_radar_rows=leader_rows,
        leader_radar_symbols=[str(row.get("code", "")).strip() for row in leader_rows if row.get("code")],
        theme_radar_current=theme_current,
        theme_radar=theme_radar,
        theme_radar_source=theme_source,
        theme_activity=theme_activity,
        theme_candidate_map=build_theme_candidate_map(theme_radar),
        mainline_candidates=mainline_candidates,
        mainline_ai_cap=mainline_cfg.max_ai_candidates,
        rps_universe_count=len(l1_input),
    )


def _structure_shadow(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    formal_triggers: dict[str, list[tuple[str, float]]],
) -> dict:
    try:
        structure = detect_structure_triggers(symbols, df_map, cfg)
        return build_structure_shadow(formal_triggers, structure, universe_count=len(symbols))
    except Exception as exc:
        logger.warning("structure shadow unavailable: %s", exc)
        return {
            "mode": "observation_only",
            "status": "unavailable",
            "affects_formal_selection": False,
            "universe_count": len(symbols),
            "reason": type(exc).__name__,
        }


def _run_strength_layers(
    l1_input: list[str],
    all_df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
    ref_data: FunnelReferenceData,
) -> tuple[list[str], list[str], dict[str, str], dict[str, str]]:
    l1_passed = layer1_filter(
        l1_input, ref_data.name_map, ref_data.market_cap_map, all_df_map, cfg, financial_map=ref_data.financial_map
    )
    l2_rejections: dict[str, str] = {}
    l2_passed, l2_channel_map, _pre_ignition = layer2_strength_detailed(
        l1_passed,
        all_df_map,
        bench_df,
        cfg,
        rps_universe=l1_input,
        rejections=l2_rejections,
    )
    return l1_passed, l2_passed, l2_channel_map, l2_rejections


def _run_sector_layer(
    l1_passed: list[str],
    l2_passed: list[str],
    all_df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    ref_data: FunnelReferenceData,
    etf_l2_passed: list[str],
    etf_sector_map: dict[str, str],
    etf_df_map: dict[str, pd.DataFrame],
    activity_hot_concepts: list[str],
    regime: str | None = None,
    benchmark_context: dict | None = None,
) -> tuple[list[str], list[str], dict]:
    etf_codes = set(etf_sector_map)
    layer3_sector_map = {**ref_data.sector_map, **etf_sector_map}
    layer3_df_map = {**all_df_map, **etf_df_map}
    l3_raw, top_sectors = layer3_sector_resonance(
        l2_passed + etf_l2_passed,
        layer3_sector_map,
        cfg,
        base_symbols=l1_passed + list(etf_codes & set(etf_df_map)),
        df_map=layer3_df_map,
        concept_map=ref_data.concept_map,
        hot_concepts=list(dict.fromkeys([*ref_data.hot_concepts, *activity_hot_concepts])),
    )
    is_repair = regime in {"PANIC_REPAIR", "PANIC_REPAIR_CONFIRMED", "PANIC_REPAIR_INTRADAY", "BEAR_REBOUND"}
    if is_repair:
        print("[funnel] 修复期风格切换第一天，Layer 3 板块共振过滤已从硬过滤降级为加分项。")
        l3_passed_normal = [s for s in l3_raw if s not in etf_codes]
        if benchmark_context is not None:
            benchmark_context["l3_passed_normal"] = l3_passed_normal
        l3_passed = [s for s in l2_passed if s not in etf_codes]
    else:
        l3_passed = [s for s in l3_raw if s not in etf_codes]
    sector_rotation = analyze_sector_rotation(
        all_df_map,
        ref_data.sector_map,
        universe_symbols=list(all_df_map.keys()),
        focus_sectors=top_sectors,
    )
    print(f"[funnel] 板块轮动温度计: {sector_rotation.get('headline', '无')}")
    return l3_passed, top_sectors, sector_rotation


def _build_theme_activity(window, ref_data: FunnelReferenceData, all_df_map: dict[str, pd.DataFrame]) -> dict:
    return build_theme_activity_snapshot(
        trade_date=window.end_trade_date.isoformat(),
        df_map=all_df_map,
        concept_map=ref_data.concept_map,
        sector_map=ref_data.sector_map,
        concept_heat=_effective_concept_heat(ref_data),
    )


def _effective_concept_heat(ref_data: FunnelReferenceData) -> list[dict]:
    return merge_concept_heat(ref_data.concept_heat, ref_data.event_concept_heat)


def _hot_concepts(ref_data: FunnelReferenceData, theme_activity: dict) -> list[str]:
    return list(dict.fromkeys([*_event_hot_concepts(ref_data), *_activity_hot_concepts(theme_activity)]))


def _event_hot_concepts(ref_data: FunnelReferenceData) -> list[str]:
    rows: list[str] = []
    for item in ref_data.event_concept_heat or []:
        name = str(item.get("name") or "").strip()
        if name and _safe_float(item.get("event_heat")) > 0:
            rows.append(name)
    return rows[:10]


def _activity_hot_concepts(theme_activity: dict) -> list[str]:
    rows: list[str] = []
    for item in theme_activity.get("themes") or []:
        theme = str(item.get("theme") or "").strip()
        if theme and _safe_float(item.get("score")) >= 0.48:
            rows.append(theme)
    return rows[:8]


def _build_theme_context(
    window,
    ref_data: FunnelReferenceData,
    all_df_map: dict[str, pd.DataFrame],
) -> tuple[dict, dict, str]:
    trade_date = window.end_trade_date.isoformat()
    current = _safe_build_theme_radar(
        trade_date=trade_date,
        concept_heat=_effective_concept_heat(ref_data),
        concept_map=ref_data.concept_map,
        sector_map=ref_data.sector_map,
        df_map=all_df_map,
        name_map=ref_data.name_map,
    )
    radar, source = _resolve_linked_theme_radar(current, trade_date)
    return current, radar, source


def _load_theme_radar_history() -> dict:
    try:
        from integrations.supabase_concept_heat import load_concept_heat_history_from_supabase

        history = load_concept_heat_history_from_supabase()
        if history:
            return history
    except Exception as exc:
        logger.debug("theme radar supabase history unavailable: %s", exc)
    try:
        if CONCEPT_HEAT_HISTORY.exists():
            with open(CONCEPT_HEAT_HISTORY, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.debug("theme radar local history unavailable: %s", exc)
    return {}


def _safe_build_theme_radar(
    *,
    trade_date: str,
    concept_heat: list[dict],
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    df_map: dict[str, pd.DataFrame],
    name_map: dict[str, str],
) -> dict:
    if not FUNNEL_THEME_RADAR_ENABLED:
        return empty_theme_snapshot(trade_date)
    try:
        return build_theme_radar_snapshot(
            trade_date=trade_date,
            concept_heat=concept_heat,
            concept_history=_load_theme_radar_history(),
            concept_map=concept_map,
            sector_map=sector_map,
            df_map=df_map,
            name_map=name_map,
        )
    except Exception as exc:
        logger.warning("theme radar build failed: %s", exc)
        return empty_theme_snapshot(trade_date)


def _resolve_linked_theme_radar(current_snapshot: dict, trade_date: str) -> tuple[dict, str]:
    persisted = None
    if FUNNEL_THEME_RADAR_ENABLED and FUNNEL_THEME_RADAR_LINK_ENABLED:
        try:
            from integrations.theme_radar_storage import load_latest_theme_radar_snapshot

            persisted = load_latest_theme_radar_snapshot()
        except Exception as exc:
            logger.debug("theme radar persisted snapshot unavailable: %s", exc)
    return select_linked_theme_radar(
        current_snapshot,
        persisted,
        trade_date,
        enabled=FUNNEL_THEME_RADAR_ENABLED,
        link_enabled=FUNNEL_THEME_RADAR_LINK_ENABLED,
        max_age_days=FUNNEL_THEME_RADAR_MAX_AGE_DAYS,
    )


def _l2_channel_counts(channel_map: dict[str, str]) -> dict[str, int]:
    return {
        "momentum": sum(1 for v in channel_map.values() if "主升通道" in v),
        "ambush": sum(1 for v in channel_map.values() if "潜伏通道" in v),
        "accum": sum(1 for v in channel_map.values() if "吸筹通道" in v),
        "dry_vol": sum(1 for v in channel_map.values() if "地量蓄势" in v),
        "rs_div": sum(1 for v in channel_map.values() if "暗中护盘" in v),
        "trend_cont": sum(1 for v in channel_map.values() if "趋势延续" in v),
        "sos": sum(1 for v in channel_map.values() if "点火破局" in v),
    }


def _build_mainline_candidates_helper(
    l1_passed: list[str],
    l2_passed: list[str],
    ref_data: FunnelReferenceData,
    theme_current: dict,
    theme_activity: dict,
    mainline_cfg: Any,
    all_df_map: dict[str, pd.DataFrame],
) -> list[dict]:
    return build_mainline_candidates(
        l1_passed=l1_passed,
        l2_passed=l2_passed,
        concept_map=ref_data.concept_map,
        sector_map=ref_data.sector_map,
        concept_heat=_effective_concept_heat(ref_data),
        theme_radar=theme_current,
        theme_activity=theme_activity,
        hot_events=ref_data.ths_hot_events,
        df_map=all_df_map,
        financial_map=ref_data.financial_map,
        name_map=ref_data.name_map,
        config=mainline_cfg,
    )
