"""Candidate pool construction workflow for the A-share funnel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import pandas as pd

from core.candidate_lanes import build_l1_candidate_lane_entries, merge_candidate_entries
from core.candidate_ranker import rank_l3_candidates
from core.funnel_theme import strategic_bypass_seed_codes
from core.mainline_engine import mainline_candidate_entries
from core.trend_drawdown_risk import annotate_trend_drawdown_risk
from core.wyckoff_engine import (
    FunnelConfig,
    build_candidate_entries,
    detect_accum_stage,
    detect_markup_stage,
    layer4_triggers,
    layer5_exit_signals,
)
from workflows.funnel_layers import FunnelLayerOutputs
from workflows.funnel_settings import (
    FUNNEL_STRATEGIC_L2_BYPASS_ENABLED,
    FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE,
    FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE,
    FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_ENABLED,
    FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FunnelStrategicBypass:
    seed_codes: list[str]
    pool: list[str]
    triggers: dict[str, list[tuple[str, float]]]
    stage_map: dict[str, str]
    markup_symbols: list[str]
    reason_map: dict[str, list[str]]
    rescue_map: dict[str, list[str]]


@dataclass(frozen=True)
class FunnelCandidateOutputs:
    markup_symbols: list[str]
    accum_stage_map: dict[str, str]
    exit_signals: dict[str, dict]
    candidate_entries: list[dict]
    mainline_candidate_entries: list[dict]
    lane_candidate_entries: list[dict]
    ranked_l3_symbols: list[str]
    l3_score_map: dict[str, float]
    total_hits: int


def trigger_hit_codes(trigger_map: dict[str, list[tuple[str, float]]]) -> set[str]:
    return {str(code).strip() for hits in (trigger_map or {}).values() for code, _ in hits if str(code).strip()}


def build_l2_bypass_pool(
    *,
    l1_passed: list[str],
    l2_passed: list[str],
    top_sectors: list[str],
    sector_map: dict[str, str],
    all_df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    channel_map: dict[str, str],
    market_cap_map: dict[str, float],
) -> tuple[list[str], dict[str, list[tuple[str, float]]]]:
    rejected = [s for s in l1_passed if s not in set(l2_passed)]
    in_sector = [s for s in rejected if str(sector_map.get(s, "")).strip() in set(top_sectors)] if top_sectors else []
    if not in_sector:
        return [], {}
    triggers = layer4_triggers(in_sector, all_df_map, cfg, channel_map=channel_map, market_cap_map=market_cap_map)
    pool = sorted(trigger_hit_codes(triggers))
    if pool:
        print(f"[funnel] 形态旁路观察池: {len(pool)} 只 (结构强度不足但有买点形态+板块共振)")
    return pool, triggers


def build_strategic_bypass_from_theme(
    *,
    layers: FunnelLayerOutputs,
    all_df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    market_cap_map: dict[str, float],
) -> FunnelStrategicBypass:
    seed_codes = strategic_bypass_seed_codes(
        layers.l1_passed,
        layers.l2_passed,
        layers.theme_candidate_map,
        enabled=FUNNEL_STRATEGIC_L2_BYPASS_ENABLED,
        min_theme_score=FUNNEL_STRATEGIC_L2_BYPASS_MIN_THEME_SCORE,
        min_stock_score=FUNNEL_STRATEGIC_L2_BYPASS_MIN_STOCK_SCORE,
    )
    raw = _build_strategic_l2_bypass(seed_codes, all_df_map, cfg, layers.l2_channel_map, market_cap_map)
    strategic = FunnelStrategicBypass(
        seed_codes=seed_codes,
        pool=list(raw.get("pool") or []),
        triggers=raw.get("triggers") or {},
        stage_map=raw.get("stage_map") or {},
        markup_symbols=raw.get("markup_symbols") or [],
        reason_map=raw.get("reason_map") or {},
        rescue_map=raw.get("rescue_map") or {},
    )
    if strategic.pool:
        print(
            "[funnel] 战略主题观察: "
            f"seeds={len(strategic.seed_codes)}, pool={len(strategic.pool)}, "
            f"buy_trigger={len(trigger_hit_codes(strategic.triggers))}, "
            f"stage={len(strategic.reason_map)}, rescue={len(strategic.rescue_map)}"
        )
    return strategic


def build_candidate_outputs(
    *,
    layers: FunnelLayerOutputs,
    strategic: FunnelStrategicBypass,
    all_df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    cfg: FunnelConfig,
) -> FunnelCandidateOutputs:
    markup_symbols = sorted(set(detect_markup_stage(layers.l3_passed, all_df_map, cfg)) | set(strategic.markup_symbols))
    accum_stage_map = detect_accum_stage(layers.l2_passed, all_df_map, cfg)
    accum_stage_map.update(strategic.stage_map)
    exit_signals = layer5_exit_signals(
        sorted(set(layers.l2_passed + markup_symbols + strategic.pool)),
        all_df_map,
        accum_stage_map,
        cfg,
    )
    candidate_entries = build_candidate_entries(
        alpha_symbols=layers.l1_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        channel_map=layers.l2_channel_map,
        triggers=layers.triggers,
        stage_map={**accum_stage_map, **{code: "Markup" for code in markup_symbols}},
        exit_signals=exit_signals,
        cfg=cfg,
    )
    lane_entries = build_l1_candidate_lane_entries(
        l1_symbols=layers.l1_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        top_sectors=layers.top_sectors,
        l2_symbols=layers.l2_passed,
        channel_map=layers.l2_channel_map,
    )
    mainline_entries = mainline_candidate_entries(
        layers.mainline_candidates,
        max_count=layers.mainline_ai_cap,
    )
    candidate_entries = merge_candidate_entries(candidate_entries, lane_entries, mainline_entries)
    annotate_trend_drawdown_risk(candidate_entries, all_df_map, layers.l2_channel_map)
    ranked_l3_symbols, l3_score_map = rank_l3_candidates(
        l3_symbols=layers.l3_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        triggers=layers.triggers,
        top_sectors=layers.top_sectors,
        l2_channel_map=layers.l2_channel_map,
        sector_rotation_map=(layers.sector_rotation.get("state_map", {}) or {}),
    )
    return FunnelCandidateOutputs(
        markup_symbols=markup_symbols,
        accum_stage_map=accum_stage_map,
        exit_signals=exit_signals,
        candidate_entries=candidate_entries,
        mainline_candidate_entries=mainline_entries,
        lane_candidate_entries=lane_entries,
        ranked_l3_symbols=ranked_l3_symbols,
        l3_score_map=l3_score_map,
        total_hits=sum(len(v) for v in layers.triggers.values()),
    )


def _strategic_stage_reason_map(stage_map: dict[str, str], markup_symbols: list[str]) -> dict[str, list[str]]:
    reasons = {str(code).strip(): ["战略阶段:Markup"] for code in markup_symbols if str(code).strip()}
    for code, stage in stage_map.items():
        code_s = str(code).strip()
        stage_s = str(stage or "").strip()
        if code_s and stage_s in {"Accum_B", "Accum_C"}:
            reasons.setdefault(code_s, []).append(f"战略阶段:{stage_s}")
    return reasons


def _fetch_rescue_klines(seed_codes: list[str]) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    empty: tuple[dict, dict] = ({}, {})
    if not FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_ENABLED or not seed_codes:
        return empty
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        return empty
    try:
        from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol

        symbols = [normalize_cn_symbol(code) for code in seed_codes]
        symbols = [s for s in symbols if s]
        if not symbols:
            return empty
        client = TickFlowClient(api_key=api_key)
        df_60m_map = client.get_klines_batch(symbols, period="60m", count=100)
        df_30m_map = client.get_klines_batch(symbols, period="30m", count=100)
        return df_60m_map, df_30m_map
    except Exception as exc:
        logger.warning("60m/30m rescue klines fetch failed: %s", exc)
        return empty


def _rescue_structure_reason_map(
    seed_codes: list[str],
    df_60m_map: dict[str, pd.DataFrame],
    df_30m_map: dict[str, pd.DataFrame] | None = None,
) -> dict[str, list[str]]:
    if not df_60m_map:
        return {}
    from core.intraday_analysis import analyze_rescue_structure
    from integrations.tickflow_client import normalize_cn_symbol

    threshold = FUNNEL_STRATEGIC_L2_BYPASS_RESCUE_MIN_SCORE
    df_30m_map = df_30m_map or {}
    result: dict[str, list[str]] = {}
    for code in seed_codes:
        sym = normalize_cn_symbol(code)
        df_60 = df_60m_map.get(sym)
        if df_60 is None or getattr(df_60, "empty", True):
            continue
        df_30 = df_30m_map.get(sym)
        rescue = analyze_rescue_structure(df_60, df_30)
        if rescue.rescue_score >= threshold:
            result[code] = [f"60m结构救援({rescue.rescue_score:.0f}分)", *rescue.rescue_reasons]
    return result


def _build_strategic_l2_bypass(
    seed_codes: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    channel_map: dict[str, str],
    market_cap_map: dict[str, float],
) -> dict:
    if not seed_codes:
        return {"pool": [], "triggers": {}, "stage_map": {}, "markup_symbols": [], "reason_map": {}, "rescue_map": {}}
    trigger_map = layer4_triggers(seed_codes, df_map, cfg, channel_map=channel_map, market_cap_map=market_cap_map)
    stage_map = detect_accum_stage(seed_codes, df_map, cfg)
    markup_symbols = detect_markup_stage(seed_codes, df_map, cfg)
    reason_map = _strategic_stage_reason_map(stage_map, markup_symbols)
    df_60m_map, df_30m_map = _fetch_rescue_klines(seed_codes)
    rescue_reason_map = _rescue_structure_reason_map(seed_codes, df_60m_map, df_30m_map)
    for code, reasons in rescue_reason_map.items():
        reason_map.setdefault(code, []).extend(reasons)
    pool = sorted(trigger_hit_codes(trigger_map) | set(reason_map))
    return {
        "pool": pool,
        "triggers": trigger_map,
        "stage_map": stage_map,
        "markup_symbols": markup_symbols,
        "reason_map": reason_map,
        "rescue_map": rescue_reason_map,
    }
