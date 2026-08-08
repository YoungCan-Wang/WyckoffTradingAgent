"""Mainline theme engine for A-share funnel candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from core._price_math import clamp as _clamp
from core._price_math import day_close_pos as _day_close_pos
from core._price_math import drawdown_pct as _drawdown_pct
from core._price_math import numeric_column as _numeric_series
from core._price_math import range_pos as _range_pos
from core._price_math import ret_pct as _ret_pct
from core._price_math import sort_by_date_if_needed
from core._price_math import upper_shadow_pct as _upper_shadow_pct
from core._price_math import vol_ratio as _vol_ratio
from core.candidate_metadata import code6
from core.main_force_signal import MainForceSignal, analyze_main_force_signal
from core.theme_radar import normalize_theme_name
from utils.safe import safe_float as _safe_float

MAINLINE_BUY_STATUS = "主线买点候选"
MAINLINE_DIVERGENCE_STATUS = "强主线分歧"
MAINLINE_EVENT_REVERSAL_STATUS = "事件主题修复候选"
MAINLINE_THEME_REVERSAL_STATUS = "主题修复候选"
MAINLINE_OBSERVE_STATUS = "主线观察"
MAINLINE_AVOID_STATUS = "过热不追"
TRADEABLE_MAINLINE_STATUSES = frozenset(
    {
        MAINLINE_BUY_STATUS,
        MAINLINE_DIVERGENCE_STATUS,
        MAINLINE_EVENT_REVERSAL_STATUS,
        MAINLINE_THEME_REVERSAL_STATUS,
    }
)
BLOCKING_TIMING_FLAGS = {"鱼尾加速", "放量长上影", "跌破确认支撑", "主题缩量阴跌"}


@dataclass(frozen=True)
class MainlineEngineConfig:
    enabled: bool = True
    max_ai_candidates: int = 3
    min_theme_score: float = 0.55
    min_stock_score: float = 0.60
    min_timing_score: float = 0.55
    allow_l2_bypass: bool = True
    allow_l4_bypass: bool = False
    max_candidates_per_theme: int = 8
    themes: tuple[str, ...] = ()
    core_basket: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class MainlineCandidate:
    code: str
    name: str
    theme: str
    status: str
    entry_type: str
    mainline_score: float
    theme_score: float
    stock_role_score: float
    quality_score: float
    timing_score: float
    l2_passed: bool
    source: str
    theme_source: str
    theme_event_id: str
    theme_event_date: str
    theme_event_title: str
    theme_event_heat: float
    theme_event_reason: str
    reasons: list[str]
    risk_flags: list[str]
    metrics: dict[str, float]


def build_mainline_candidates(
    *,
    l1_passed: list[str],
    l2_passed: list[str],
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str] | None = None,
    concept_heat: list[dict[str, Any]],
    theme_radar: dict[str, Any],
    theme_activity: dict[str, Any] | None = None,
    df_map: dict[str, pd.DataFrame],
    financial_map: dict[str, dict],
    name_map: dict[str, str],
    main_force_map: dict[str, MainForceSignal] | None = None,
    hot_events: dict[str, Any] | None = None,
    config: MainlineEngineConfig | None = None,
) -> list[dict[str, Any]]:
    cfg = config or MainlineEngineConfig()
    if not cfg.enabled:
        return []
    l1_set = {str(code).strip() for code in l1_passed if str(code).strip()}
    l2_set = {str(code).strip() for code in l2_passed if str(code).strip()}
    theme_scores = _theme_scores(concept_heat, theme_radar, theme_activity or {}, hot_events or {}, cfg)
    seeds = _mainline_seed_map(
        l1_set,
        concept_map,
        sector_map or {},
        theme_scores,
        theme_radar,
        hot_events or {},
        cfg,
    )
    candidates = [
        _candidate_from_seed(
            code, seed, l2_set, df_map, financial_map, name_map, theme_scores, cfg, (main_force_map or {}).get(code)
        )
        for code, seed in seeds.items()
        if code in df_map and (cfg.allow_l2_bypass or code in l2_set)
    ]
    return [asdict(item) for item in _rank_candidates([c for c in candidates if c is not None], cfg)]


def mainline_candidate_entries(candidates: list[dict[str, Any]], *, max_count: int) -> list[dict[str, Any]]:
    tradeable = [item for item in candidates if str(item.get("status")) in TRADEABLE_MAINLINE_STATUSES]
    ranked = sorted(tradeable, key=lambda item: (-float(item.get("mainline_score") or 0), str(item.get("code"))))
    rows = ranked if max_count <= 0 else ranked[:max_count]
    return [_candidate_entry(item) for item in rows]


def _mainline_seed_map(
    l1_set: set[str],
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    theme_scores: dict[str, float],
    theme_radar: dict[str, Any],
    hot_events: dict[str, Any],
    cfg: MainlineEngineConfig,
) -> dict[str, dict[str, Any]]:
    seeds: dict[str, dict[str, Any]] = {}
    _add_core_basket_seeds(seeds, l1_set, cfg)
    _add_radar_seeds(seeds, l1_set, concept_map, sector_map, theme_radar, cfg)
    _add_hot_event_seeds(seeds, l1_set, hot_events, cfg)
    _add_concept_seeds(seeds, l1_set, concept_map, theme_scores, cfg)
    return seeds


def _add_core_basket_seeds(seeds: dict[str, dict[str, Any]], l1_set: set[str], cfg: MainlineEngineConfig) -> None:
    for code, name, theme in cfg.core_basket:
        if code in l1_set:
            seeds.setdefault(code, {"theme": theme, "source": "core_basket", "name": name})


def _add_radar_seeds(
    seeds: dict[str, dict[str, Any]],
    l1_set: set[str],
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    theme_radar: dict[str, Any],
    cfg: MainlineEngineConfig,
) -> None:
    for item in theme_radar.get("strategic_candidates") or []:
        code = str(item.get("code", "")).strip()
        theme = _mainline_theme(item.get("theme"), cfg)
        evidence = _current_theme_evidence(code, theme, concept_map, sector_map, cfg)
        if code in l1_set and theme and evidence:
            seeds[code] = {
                "theme": theme,
                "source": "theme_radar",
                "radar": item,
                "theme_membership_evidence": evidence,
            }


def _current_theme_evidence(
    code: str,
    theme: str,
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    cfg: MainlineEngineConfig,
) -> list[str]:
    labels = [*(concept_map.get(code) or []), sector_map.get(code, "")]
    return [str(label) for label in labels if label and _mainline_theme(label, cfg) == theme]


def _add_concept_seeds(
    seeds: dict[str, dict[str, Any]],
    l1_set: set[str],
    concept_map: dict[str, list[str]],
    theme_scores: dict[str, float],
    cfg: MainlineEngineConfig,
) -> None:
    active = {theme for theme, score in theme_scores.items() if score >= 0.40}
    for code, concepts in concept_map.items():
        code_s = str(code).strip()
        if code_s not in l1_set or code_s in seeds:
            continue
        for concept in concepts or []:
            theme = _mainline_theme(concept, cfg)
            if theme and theme in active:
                seeds[code_s] = {"theme": theme, "source": "concept_map"}
                break


def _add_hot_event_seeds(
    seeds: dict[str, dict[str, Any]],
    l1_set: set[str],
    hot_events: dict[str, Any],
    cfg: MainlineEngineConfig,
) -> None:
    for event in hot_events.get("events") or []:
        theme = _mainline_theme(event.get("theme") or event.get("investment_direction"), cfg)
        if not theme:
            continue
        for stock in event.get("stocks") or []:
            code = str(stock.get("code") or "").strip()
            if code in l1_set:
                seeds[code] = _hot_event_seed(event, stock, theme)


def _hot_event_seed(event: dict[str, Any], stock: dict[str, Any], theme: str) -> dict[str, Any]:
    return {
        "theme": theme,
        "source": "ths_hot_event",
        "theme_source": "ths_hot_event",
        "name": str(stock.get("name") or ""),
        "event": event,
        "stock_reason": str(stock.get("reason") or ""),
    }


def _is_hot_event_seed(seed: dict[str, Any]) -> bool:
    return str(seed.get("source") or "") == "ths_hot_event"


def _seed_event_text(seed: dict[str, Any], key: str) -> str:
    return str((seed.get("event") or {}).get(key) or "").strip()


def _candidate_from_seed(
    code: str,
    seed: dict[str, Any],
    l2_set: set[str],
    df_map: dict[str, pd.DataFrame],
    financial_map: dict[str, dict],
    name_map: dict[str, str],
    theme_scores: dict[str, float],
    cfg: MainlineEngineConfig,
    main_force: MainForceSignal | None,
) -> MainlineCandidate | None:
    metrics = _price_metrics(df_map.get(code), main_force)
    if not metrics:
        return None
    theme = str(seed.get("theme") or "")
    radar = seed.get("radar") or {}
    theme_score = _seed_theme_score(seed, theme, theme_scores, radar)
    role_score = _stock_role_score(metrics, radar, seed.get("source") == "core_basket", _is_hot_event_seed(seed))
    quality_score = _quality_score(_lookup_financial(financial_map, code))
    timing_score, entry_type, risk_flags = _timing_result(metrics, has_event=_is_hot_event_seed(seed))
    mainline_score = _score(theme_score, role_score, quality_score, timing_score)
    status = _status(theme_score, role_score, timing_score, entry_type, risk_flags, cfg)
    return MainlineCandidate(
        code=code,
        name=str(seed.get("name") or name_map.get(code) or radar.get("name") or code),
        theme=theme,
        status=status,
        entry_type=entry_type,
        mainline_score=round(mainline_score, 4),
        theme_score=round(theme_score, 4),
        stock_role_score=round(role_score, 4),
        quality_score=round(quality_score, 4),
        timing_score=round(timing_score, 4),
        l2_passed=code in l2_set,
        source=str(seed.get("source") or "mainline"),
        theme_source=str(seed.get("theme_source") or seed.get("source") or ""),
        theme_event_id=_seed_event_text(seed, "event_id"),
        theme_event_date=_seed_event_text(seed, "trade_date"),
        theme_event_title=_seed_event_text(seed, "title"),
        theme_event_heat=round(_safe_float((seed.get("event") or {}).get("heat")) or 0.0, 2),
        theme_event_reason=str(seed.get("stock_reason") or ""),
        reasons=_reasons(theme, theme_score, role_score, quality_score, timing_score, entry_type, metrics, seed),
        risk_flags=risk_flags,
        metrics=metrics,
    )


def _rank_candidates(candidates: list[MainlineCandidate], cfg: MainlineEngineConfig) -> list[MainlineCandidate]:
    status_rank = {
        MAINLINE_BUY_STATUS: 0,
        MAINLINE_DIVERGENCE_STATUS: 1,
        MAINLINE_EVENT_REVERSAL_STATUS: 2,
        MAINLINE_THEME_REVERSAL_STATUS: 2,
        MAINLINE_OBSERVE_STATUS: 3,
        MAINLINE_AVOID_STATUS: 4,
    }
    ranked = sorted(candidates, key=lambda item: (status_rank.get(item.status, 9), -item.mainline_score, item.code))
    buckets: dict[str, int] = {}
    out: list[MainlineCandidate] = []
    for item in ranked:
        count = buckets.get(item.theme, 0)
        if count < cfg.max_candidates_per_theme or item.source == "core_basket":
            out.append(item)
            buckets[item.theme] = count + 1
    return out


def _seed_theme_score(seed: dict[str, Any], theme: str, theme_scores: dict[str, float], radar: dict[str, Any]) -> float:
    score = max(float(theme_scores.get(theme, 0.0)), _safe_float(radar.get("theme_score")))
    return max(score, 0.55) if seed.get("source") == "core_basket" else score


def _theme_scores(
    concept_heat: list[dict[str, Any]],
    theme_radar: dict[str, Any],
    theme_activity: dict[str, Any],
    hot_events: dict[str, Any],
    cfg: MainlineEngineConfig,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for item in theme_radar.get("themes") or []:
        theme = _mainline_theme(item.get("theme"), cfg)
        if theme:
            scores[theme] = max(scores.get(theme, 0.0), _safe_float(item.get("score")))
    for item in theme_radar.get("strategic_candidates") or []:
        theme = _mainline_theme(item.get("theme"), cfg)
        if theme:
            scores[theme] = max(scores.get(theme, 0.0), _safe_float(item.get("theme_score")))
    for rank, item in enumerate(concept_heat or [], start=1):
        theme = _mainline_theme(item.get("name"), cfg)
        if theme:
            scores[theme] = max(scores.get(theme, 0.0), _heat_score(item, rank))
    for item in theme_activity.get("themes") or []:
        theme = _mainline_theme(item.get("theme"), cfg)
        if theme:
            scores[theme] = max(scores.get(theme, 0.0), _safe_float(item.get("score")))
    for rank, event in enumerate(hot_events.get("events") or [], start=1):
        for theme in _hot_event_themes(event, cfg):
            scores[theme] = max(scores.get(theme, 0.0), _hot_event_score(event, rank))
    return scores


def _hot_event_themes(event: dict[str, Any], cfg: MainlineEngineConfig) -> list[str]:
    themes: list[str] = []
    for raw in [event.get("theme"), event.get("investment_direction")]:
        theme = _mainline_theme(raw, cfg)
        if theme and theme not in themes:
            themes.append(theme)
    for row in event.get("themes") or []:
        theme = _mainline_theme(row.get("theme") or row.get("name"), cfg)
        if theme and theme not in themes:
            themes.append(theme)
    return themes


def _hot_event_score(event: dict[str, Any], rank: int) -> float:
    heat_score = _clamp(_safe_float(event.get("heat")) / 250_000.0)
    pct_score = _clamp((_safe_float(event.get("rise_pct")) + 1.0) / 8.0)
    limit_score = _clamp(_safe_float(event.get("limit_up_count")) / 30.0)
    rank_score = _clamp(1.0 - (rank - 1.0) / 20.0)
    return _clamp(0.38 * heat_score + 0.27 * pct_score + 0.25 * limit_score + 0.10 * rank_score)


def _mainline_theme(raw: Any, cfg: MainlineEngineConfig) -> str:
    theme = normalize_theme_name(str(raw or ""))
    return theme if theme and (not cfg.themes or theme in set(cfg.themes)) else ""


def _price_metrics(df: pd.DataFrame | None, main_force: MainForceSignal | None = None) -> dict[str, float]:
    if df is None or df.empty or "close" not in df.columns:
        return {}
    ordered = sort_by_date_if_needed(df)
    close = pd.to_numeric(ordered["close"], errors="coerce").dropna()
    if len(close) < 60:
        return {}
    volume = _numeric_series(ordered, "volume")
    amount = _numeric_series(ordered, "amount")
    high = _numeric_series(ordered, "high")
    low = _numeric_series(ordered, "low")
    open_ = _numeric_series(ordered, "open")
    last = float(close.iloc[-1])
    ma5 = float(close.tail(5).mean()) if len(close) >= 5 else last
    ma10 = float(close.tail(10).mean()) if len(close) >= 10 else last
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    high20 = float(high.tail(20).max()) if not high.empty else float(close.tail(20).max())
    low20 = float(low.tail(20).min()) if not low.empty else float(close.tail(20).min())
    return {
        "ret5": _ret_pct(close, 5),
        "ret20": _ret_pct(close, 20),
        "ret60": _ret_pct(close, 60),
        "ret120": _ret_pct(close, 120),
        "dist_ma5": (last / ma5 - 1.0) * 100.0 if ma5 > 0 else 0.0,
        "dist_ma10": (last / ma10 - 1.0) * 100.0 if ma10 > 0 else 0.0,
        "dist_ma20": (last / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0,
        "dist_ma50": (last / ma50 - 1.0) * 100.0 if ma50 > 0 else 0.0,
        "drawdown60": _drawdown_pct(close, 60),
        "close_pos20": _range_pos(last, low20, high20),
        "close_pos_day": _day_close_pos(ordered["close"], high, low),
        "upper_shadow_pct": _upper_shadow_pct(ordered, open_, high, close),
        "vol_ratio_5_20": _vol_ratio(volume),
        "amount20_wan": _amount20_wan(amount),
        **(main_force or analyze_main_force_signal(ordered)).metrics,
    }


def _timing_result(metrics: dict[str, float], *, has_event: bool = False) -> tuple[float, str, list[str]]:
    risk_flags = _timing_risks(metrics)
    score = _timing_score(metrics, risk_flags)
    entries = _entry_types(metrics, risk_flags, has_event=has_event)
    return score, " + ".join(entries), risk_flags


def _timing_risks(metrics: dict[str, float]) -> list[str]:
    risks: list[str] = []
    if _fish_tail_risk(metrics):
        risks.append("鱼尾加速")
    elif metrics["dist_ma20"] > 22 or metrics["ret20"] > 50:
        risks.append("高位抱团")
    if metrics["upper_shadow_pct"] > 5 and metrics["vol_ratio_5_20"] > 1.8 and metrics["close_pos_day"] < 0.45:
        risks.append("放量长上影")
    if metrics["dist_ma20"] < -5 or metrics["dist_ma50"] < -8:
        risks.append("跌破确认支撑")
    if metrics["ret20"] < -8 and metrics["vol_ratio_5_20"] < 0.9:
        risks.append("主题缩量阴跌")
    if metrics.get("main_force_score", 0.0) < 0.30 and metrics["vol_ratio_5_20"] > 1.3:
        risks.append("供给未消化")
    return risks


def _entry_types(metrics: dict[str, float], risk_flags: list[str], *, has_event: bool = False) -> list[str]:
    entries: list[str] = []
    if _event_reversal_entry_ok(metrics, risk_flags):
        entries.append("事件主题低位修复" if has_event else "主题低位修复")
    if any(flag in risk_flags for flag in BLOCKING_TIMING_FLAGS if flag != "跌破确认支撑"):
        return entries
    if "跌破确认支撑" in risk_flags and not entries:
        return []
    if _main_force_entry_ok(metrics, risk_flags):
        entries.append("主力资金进场确认")
    if _main_force_absorption_ok(metrics, risk_flags):
        entries.append("主力缩量承接")
    entries.extend(_mainline_pullback_entries(metrics))
    if metrics["close_pos20"] >= 0.86 and 4 <= metrics["ret20"] <= 36 and 0.75 <= metrics["vol_ratio_5_20"] <= 2.2:
        entries.append("主线平台再突破")
    if metrics["dist_ma20"] >= -1 and metrics["vol_ratio_5_20"] <= 0.90 and -6 <= metrics["ret5"] <= 6:
        entries.append("主线缩量强承接")
    entries.extend(_extended_mainline_entries(metrics, risk_flags))
    return entries


def _mainline_pullback_entries(metrics: dict[str, float]) -> list[str]:
    """A 股主升段优先认 MA5/MA10 回踩，再认 MA20。"""
    entries: list[str] = []
    vol = metrics["vol_ratio_5_20"]
    if (
        -1.0 <= metrics.get("dist_ma5", 99.0) <= 4.0
        and metrics["dist_ma20"] >= -2.0
        and metrics["ret20"] >= 0.0
        and vol <= 1.8
    ):
        entries.append("主线回踩MA5")
    if (
        -2.0 <= metrics.get("dist_ma10", 99.0) <= 5.0
        and metrics["dist_ma20"] >= -4.0
        and metrics["ret60"] >= 5.0
        and vol <= 1.9
    ):
        entries.append("主线回踩MA10")
    if -2 <= metrics["dist_ma20"] <= 8 and metrics["dist_ma50"] >= -2 and metrics["ret60"] >= 8:
        entries.append("主线回踩MA20")
    return entries


def _extended_mainline_entries(metrics: dict[str, float], risk_flags: list[str]) -> list[str]:
    if "高位抱团" not in risk_flags or not _high_mainline_support(metrics):
        return []
    entries: list[str] = []
    if -8 <= metrics["ret5"] <= 12 and metrics["vol_ratio_5_20"] <= 1.45:
        entries.append("主线高位横盘承接")
    if metrics["close_pos20"] >= 0.78 and metrics["ret20"] <= 65 and metrics["close_pos_day"] >= 0.55:
        entries.append("主线分歧转强")
    return entries


def _timing_score(metrics: dict[str, float], risk_flags: list[str]) -> float:
    near_short_ma = float(metrics.get("dist_ma5", 99.0) <= 4.0 or metrics.get("dist_ma10", 99.0) <= 5.0)
    trend = 0.16 * float(metrics["dist_ma20"] >= -2) + 0.12 * float(metrics["dist_ma50"] >= -3) + 0.12 * near_short_ma
    strength = 0.20 * _clamp((metrics["ret60"] - 5) / 45) + 0.15 * _clamp(metrics["close_pos20"])
    distance = 0.10 * _clamp(1.0 - max(metrics["dist_ma20"] - 12.0, 0.0) / 18.0)
    volume = 0.10 * _clamp(1.0 - max(metrics["vol_ratio_5_20"] - 1.6, 0.0) / 1.4)
    extension = 0.12 * float(_high_mainline_support(metrics))
    event_reversal = 0.16 * float(_event_reversal_entry_ok(metrics, risk_flags))
    main_force = 0.16 * _clamp(metrics.get("main_force_score", 0.0))
    return _clamp(trend + strength + distance + volume + extension + event_reversal + main_force)


def _main_force_entry_ok(metrics: dict[str, float], risk_flags: list[str]) -> bool:
    if {"鱼尾加速", "放量长上影", "跌破确认支撑"} & set(risk_flags):
        return False
    return (
        metrics.get("main_force_score", 0.0) >= 0.66
        and metrics.get("demand_supply_ratio", 0.0) >= 1.15
        and metrics["dist_ma20"] <= 22
        and metrics["close_pos_day"] >= 0.55
    )


def _main_force_absorption_ok(metrics: dict[str, float], risk_flags: list[str]) -> bool:
    if {"鱼尾加速", "放量长上影", "跌破确认支撑"} & set(risk_flags):
        return False
    return (
        metrics.get("down_amount_ratio_10_20", 9.0) <= 0.90
        and metrics.get("support_hold_score", 0.0) >= 0.66
        and metrics.get("close_pos5", 0.0) >= 0.55
        and -6 <= metrics["ret5"] <= 8
    )


def _event_reversal_entry_ok(metrics: dict[str, float], risk_flags: list[str]) -> bool:
    if "鱼尾加速" in risk_flags or "放量长上影" in risk_flags:
        return False
    return (
        metrics["amount20_wan"] >= 12000.0
        and -28.0 <= metrics["ret20"] <= 18.0
        and -45.0 <= metrics["ret60"] <= 55.0
        and -16.0 <= metrics["dist_ma20"] <= 8.0
        and -28.0 <= metrics["dist_ma50"] <= 12.0
        and metrics["close_pos20"] >= 0.22
        and 0.60 <= metrics["vol_ratio_5_20"] <= 2.60
    )


def _fish_tail_risk(metrics: dict[str, float]) -> bool:
    return (
        metrics["dist_ma20"] > 35
        or metrics["ret20"] > 70
        or (metrics["ret20"] > 55 and metrics["vol_ratio_5_20"] > 2.0)
        or (metrics["upper_shadow_pct"] > 5 and metrics["vol_ratio_5_20"] > 1.8 and metrics["close_pos_day"] < 0.45)
    )


def _high_mainline_support(metrics: dict[str, float]) -> bool:
    return (
        metrics["ret60"] >= 25
        and metrics["dist_ma50"] >= 5
        and metrics["close_pos20"] >= 0.55
        and metrics["drawdown60"] <= 18
        and metrics["vol_ratio_5_20"] <= 2.0
    )


def _stock_role_score(metrics: dict[str, float], radar: dict[str, Any], is_core: bool, is_event: bool) -> float:
    radar_score = _safe_float(radar.get("stock_score"))
    absolute_score = (
        0.30 * _clamp((metrics["ret60"] + 5.0) / 45.0)
        + 0.25 * _clamp((metrics["ret120"] + 10.0) / 80.0)
        + 0.20 * _clamp(1.0 - metrics["drawdown60"] / 24.0)
        + 0.15 * _clamp(metrics["close_pos20"])
        + 0.10 * float(metrics["dist_ma50"] >= 0)
        + 0.10 * _clamp(metrics.get("main_force_score", 0.0))
    )
    return _clamp(max(radar_score, absolute_score) + (0.10 if is_core else 0.0) + (0.06 if is_event else 0.0))


def _quality_score(metrics: dict | None) -> float:
    if not metrics:
        return 0.55
    parts = [
        _metric_score(metrics, ("roe", "roe_weighted", "roe_diluted"), 0.0, 14.0, reverse=False),
        _metric_score(metrics, ("net_income_yoy", "netprofit_yoy"), -15.0, 35.0, reverse=False),
        _metric_score(metrics, ("revenue_yoy", "or_yoy"), -10.0, 30.0, reverse=False),
        _metric_score(metrics, ("gross_margin", "grossprofit_margin"), 12.0, 40.0, reverse=False),
        _metric_score(metrics, ("debt_to_asset_ratio", "debt_to_assets", "debt_ratio"), 85.0, 45.0, reverse=True),
        _metric_score(metrics, ("operating_cash_to_revenue", "ocf_to_or"), -5.0, 12.0, reverse=False),
    ]
    valid = [x for x in parts if x is not None]
    return round(sum(valid) / len(valid), 4) if valid else 0.55


def _candidate_entry(item: dict[str, Any]) -> dict[str, Any]:
    score = round(float(item.get("mainline_score") or 0.0) * 100.0, 2)
    status = str(item.get("status") or MAINLINE_BUY_STATUS)
    return {
        "code": item["code"],
        "track": "trend",
        "signal_key": "mainline",
        "entry_type": str(item.get("entry_type") or "mainline"),
        "lane": "mainline",
        "score": score,
        "opportunity": _candidate_opportunity(item),
        "timing": str(item.get("entry_type") or ""),
        "risk": " / ".join([status, *list(item.get("risk_flags") or [])]) or "仍需 confirmed 确认",
        "state": "Mainline",
        "reasons": list(item.get("reasons") or [])[:5],
        "metrics": _candidate_entry_metrics(item),
    }


def _candidate_opportunity(item: dict[str, Any]) -> str:
    theme = str(item.get("theme") or "")
    if item.get("theme_source") == "ths_hot_event":
        return f"事件主线: {theme}"
    return f"主线核心票: {theme}"


def _candidate_entry_metrics(item: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(item.get("metrics") or {})
    for key in (
        "theme_source",
        "theme_event_id",
        "theme_event_date",
        "theme_event_title",
        "theme_event_heat",
        "theme_event_reason",
    ):
        if item.get(key) not in (None, "", [], {}):
            metrics[key] = item.get(key)
    return metrics


def _reasons(
    theme: str,
    theme_score: float,
    role_score: float,
    quality: float,
    timing: float,
    entry: str,
    metrics: dict[str, float],
    seed: dict[str, Any],
) -> list[str]:
    rows = [
        f"主题:{theme}({theme_score:.2f})",
        f"核心度:{role_score:.2f}",
        f"质量:{quality:.2f}",
        f"时机:{timing:.2f}",
        f"主力:{metrics.get('main_force_score', 0.0):.2f}/供需{metrics.get('demand_supply_ratio', 0.0):.2f}",
        entry or "等待买点确认",
    ]
    if _is_hot_event_seed(seed):
        event = seed.get("event") or {}
        rows.insert(1, f"事件:{event.get('title') or event.get('theme')}")
    elif seed.get("theme_membership_evidence"):
        rows.insert(1, "主题依据:" + "/".join(seed["theme_membership_evidence"][:3]))
    return rows


def _status(
    theme_score: float,
    role_score: float,
    timing_score: float,
    entry_type: str,
    risk_flags: list[str],
    cfg: MainlineEngineConfig,
) -> str:
    if "鱼尾加速" in risk_flags:
        return MAINLINE_AVOID_STATUS
    if _high_divergence_is_tradeable(theme_score, role_score, timing_score, entry_type, risk_flags, cfg):
        return MAINLINE_DIVERGENCE_STATUS
    if _event_reversal_is_tradeable(theme_score, timing_score, entry_type, risk_flags, cfg):
        if "事件主题低位修复" in entry_type:
            return MAINLINE_EVENT_REVERSAL_STATUS
        return MAINLINE_THEME_REVERSAL_STATUS
    if (
        theme_score >= cfg.min_theme_score
        and role_score >= cfg.min_stock_score
        and timing_score >= cfg.min_timing_score
        and entry_type
    ):
        return MAINLINE_BUY_STATUS
    return MAINLINE_OBSERVE_STATUS


def _event_reversal_is_tradeable(
    theme_score: float,
    timing_score: float,
    entry_type: str,
    risk_flags: list[str],
    cfg: MainlineEngineConfig,
) -> bool:
    return (
        ("事件主题低位修复" in entry_type or "主题低位修复" in entry_type)
        and theme_score >= max(cfg.min_theme_score - 0.05, 0.45)
        and timing_score >= max(cfg.min_timing_score * 0.45, 0.24)
        and not {"鱼尾加速", "放量长上影"} & set(risk_flags)
    )


def _high_divergence_is_tradeable(
    theme_score: float,
    role_score: float,
    timing_score: float,
    entry_type: str,
    risk_flags: list[str],
    cfg: MainlineEngineConfig,
) -> bool:
    return (
        "高位抱团" in risk_flags
        and theme_score >= cfg.min_theme_score
        and role_score >= cfg.min_stock_score
        and timing_score >= cfg.min_timing_score * 0.8
        and bool(entry_type)
    )


def _metric_score(metrics: dict, keys: tuple[str, ...], weak: float, strong: float, *, reverse: bool) -> float | None:
    value = next(
        (_safe_float(metrics.get(key), None) for key in keys if _safe_float(metrics.get(key), None) is not None), None
    )
    if value is None:
        return None
    if reverse:
        return _clamp((weak - value) / (weak - strong))
    return _clamp((value - weak) / (strong - weak))


def _lookup_financial(financial_map: dict[str, dict], code: str) -> dict | None:
    return financial_map.get(code6(code))


def _amount20_wan(amount: pd.Series) -> float:
    if len(amount) < 20:
        return 0.0
    return float(amount.tail(20).mean()) / 10000.0


def _score(theme: float, role: float, quality: float, timing: float) -> float:
    return _clamp(0.30 * theme + 0.25 * role + 0.20 * quality + 0.25 * timing)


def _heat_score(item: dict[str, Any], rank: int) -> float:
    if item.get("source") == "ths_hot_event" or item.get("event_heat"):
        event_like = {
            "heat": item.get("event_heat"),
            "rise_pct": item.get("pct"),
            "limit_up_count": item.get("limit_up_count"),
        }
        return _hot_event_score(event_like, rank)
    pct = _safe_float(item.get("pct"))
    flow = max(_safe_float(item.get("net_inflow")) / 1e8, 0.0)
    return _clamp(0.45 * ((pct + 2.0) / 10.0) + 0.35 * min(flow / 8.0, 1.0) + 0.20 * (1.0 - rank / 40.0))
