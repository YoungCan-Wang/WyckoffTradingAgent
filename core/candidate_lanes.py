"""Candidate lane builders shared by live funnel and backtest replay."""

from __future__ import annotations

from typing import Any

import pandas as pd

from core._price_math import clamp as _clamp
from core._price_math import day_close_pos as _day_close_pos
from core._price_math import dist_pct as _dist_pct
from core._price_math import numeric_column as _num
from core._price_math import range_pos, sort_by_date_if_needed
from core._price_math import ret_pct as _ret_pct
from core._price_math import upper_shadow_pct as _upper_shadow_pct
from core._price_math import vol_ratio as _vol_ratio
from core.candidate_tracks import (
    candidate_entry_key,
    candidate_entry_score,
    normalize_candidate_entry_key,
    sanitized_candidate_entry,
)
from core.main_force_signal import MainForceSignal, analyze_main_force_signal


def build_l1_candidate_lane_entries(
    *,
    l1_symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    sector_map: dict[str, str],
    top_sectors: list[str],
    l2_symbols: list[str],
    channel_map: dict[str, str],
    main_force_map: dict[str, MainForceSignal] | None = None,
    limit: int = 80,
    blocked_lanes: tuple[str, ...] | set[str] = (),
) -> list[dict[str, Any]]:
    top_sector_set = {str(item).strip() for item in top_sectors if str(item).strip()}
    l2_set = {str(item).strip() for item in l2_symbols if str(item).strip()}
    blocked = {normalize_candidate_entry_key(t) for t in (blocked_lanes or ())}
    rows = [
        _entry_for_code(
            code,
            df_map.get(code),
            sector_map.get(code, ""),
            code in l2_set,
            channel_map.get(code, ""),
            top_sector_set,
            (main_force_map or {}).get(code),
            blocked_lanes=blocked,
        )
        for code in l1_symbols
    ]
    valid = [row for row in rows if row]
    valid.sort(key=lambda item: (-candidate_entry_score(item), str(item.get("code"))))
    return valid[: max(int(limit), 0)] if limit > 0 else valid


def merge_candidate_entries(
    *groups: list[dict[str, Any]],
    blocked_types: tuple[str, ...] | set[str] = (),
) -> list[dict[str, Any]]:
    blocked = {normalize_candidate_entry_key(t) for t in (blocked_types or ())}
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            entry_type = normalize_candidate_entry_key(item.get("entry_type") or item.get("signal_key") or "")
            if blocked and entry_type in blocked:
                continue
            current = merged.get(code)
            if current is None or _entry_rank(item) < _entry_rank(current):
                merged[code] = sanitized_candidate_entry(item)
    return sorted(merged.values(), key=_entry_rank)


def _entry_for_code(
    code: str,
    df: pd.DataFrame | None,
    sector: str,
    l2_passed: bool,
    channel: str,
    top_sector_set: set[str],
    main_force: MainForceSignal | None,
    blocked_lanes: set[str] = frozenset(),
) -> dict[str, Any] | None:
    metrics = _price_metrics(df, main_force)
    if not metrics:
        return None
    risks = _risk_flags(metrics)
    if _hard_blocked(risks):
        return None
    lane = _lane(metrics, sector in top_sector_set, l2_passed, blocked_lanes=blocked_lanes)
    if not lane:
        return None
    score = _lane_score(lane, metrics, sector in top_sector_set, l2_passed)
    if score < _lane_min_score(lane):
        return None
    return _candidate_entry(code, sector, lane, score, metrics, risks, channel)


def _candidate_entry(
    code: str,
    sector: str,
    lane: str,
    score: float,
    metrics: dict[str, float],
    risks: list[str],
    channel: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "track": "trend",
        "signal_key": lane,
        "entry_type": lane,
        "score": round(score, 2),
        "metrics": {key: round(float(value), 4) for key, value in metrics.items()},
        "opportunity": _opportunity_text(lane, sector),
        "timing": _timing_text(lane, metrics),
        "risk": " / ".join(risks) or "仍需 confirmed 确认",
        "state": "Lane",
        "lane": lane,
        "channel": channel,
        "reasons": _reasons(lane, metrics, sector),
    }


def _lane(
    metrics: dict[str, float], in_top_sector: bool, l2_passed: bool, blocked_lanes: set[str] = frozenset()
) -> str:
    if "trend_breakout" not in blocked_lanes and _trend_breakout(metrics):
        return "trend_breakout"
    if "main_force_entry" not in blocked_lanes and _main_force_entry(metrics):
        return "main_force_entry"
    if "trend_lane_pullback" not in blocked_lanes and _trend_pullback(metrics):
        return "trend_lane_pullback"
    if "sector_strength" not in blocked_lanes and in_top_sector and _sector_strength(metrics):
        return "sector_strength"
    if "wyckoff_structure" not in blocked_lanes and l2_passed and _trend_follow(metrics):
        return "wyckoff_structure"
    return ""


def _trend_breakout(metrics: dict[str, float]) -> bool:
    return (
        metrics["close_pos60"] >= 0.86
        and 8 <= metrics["ret20"] <= 38
        and metrics["ret60"] >= 18
        and -2 <= metrics["dist_ma20"] <= 14
        and 0.75 <= metrics["vol_ratio_5_20"] <= 2.4
    )


def _trend_pullback(metrics: dict[str, float]) -> bool:
    return (
        metrics["ret60"] >= 12
        and metrics["ret120"] >= 10
        and -3 <= metrics["dist_ma20"] <= 7
        and metrics["dist_ma50"] >= -3
        and metrics["vol_ratio_5_20"] <= 1.10
        and metrics["close_pos20"] >= 0.42
    )


def _sector_strength(metrics: dict[str, float]) -> bool:
    return (
        4 <= metrics["ret20"] <= 34
        and metrics["ret60"] >= 10
        and metrics["dist_ma20"] <= 12
        and metrics["close_pos20"] >= 0.55
    )


def _trend_follow(metrics: dict[str, float]) -> bool:
    return metrics["ret20"] >= 6 and metrics["ret60"] >= 8 and metrics["dist_ma20"] <= 12


def _main_force_entry(metrics: dict[str, float]) -> bool:
    return (
        metrics.get("main_force_score", 0.0) >= 0.66
        and metrics.get("demand_supply_ratio", 0.0) >= 1.15
        and metrics["dist_ma20"] <= 16
        and metrics["close_pos20"] >= 0.45
        and metrics["close_pos_day"] >= 0.55
    )


def _lane_score(lane: str, metrics: dict[str, float], in_top_sector: bool, l2_passed: bool) -> float:
    base = {
        "main_force_entry": 74.0,
        "trend_breakout": 72.0,
        "trend_lane_pullback": 70.0,
        "sector_strength": 66.0,
        "wyckoff_structure": 62.0,
    }[lane]
    strength = 12.0 * _clamp(metrics["ret60"] / 45.0) + 8.0 * _clamp(metrics["close_pos20"])
    distance = 6.0 * _clamp(1.0 - max(metrics["dist_ma20"] - 6.0, 0.0) / 12.0)
    demand = 8.0 * _clamp(metrics.get("main_force_score", 0.0))
    context = (5.0 if in_top_sector else 0.0) + (3.0 if l2_passed else 0.0)
    return min(base + strength + distance + demand + context, 100.0)


def _lane_min_score(lane: str) -> float:
    return {
        "main_force_entry": 78.0,
        "trend_breakout": 78.0,
        "trend_lane_pullback": 76.0,
        "sector_strength": 74.0,
        "wyckoff_structure": 72.0,
    }[lane]


def _price_metrics(df: pd.DataFrame | None, main_force: MainForceSignal | None = None) -> dict[str, float]:
    if df is None or df.empty or "close" not in df.columns:
        return {}
    ordered = sort_by_date_if_needed(df)
    close = pd.to_numeric(ordered["close"], errors="coerce").dropna()
    if len(close) < 80:
        return {}
    high = _num(ordered, "high")
    low = _num(ordered, "low")
    open_ = _num(ordered, "open")
    volume = _num(ordered, "volume")
    last = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    return {
        "ret5": _ret_pct(close, 5),
        "ret20": _ret_pct(close, 20),
        "ret60": _ret_pct(close, 60),
        "ret120": _ret_pct(close, 120),
        "dist_ma20": _dist_pct(last, ma20),
        "dist_ma50": _dist_pct(last, ma50),
        "close_pos20": range_pos(last, float(low.tail(20).min()), float(high.tail(20).max())),
        "close_pos60": range_pos(last, float(low.tail(60).min()), float(high.tail(60).max())),
        "close_pos_day": _day_close_pos(ordered["close"], high, low, use_tail=True),
        "upper_shadow_pct": _upper_shadow_pct(ordered, open_, high, close),
        "vol_ratio_5_20": _vol_ratio(volume),
        **(main_force or analyze_main_force_signal(ordered)).metrics,
    }


def _risk_flags(metrics: dict[str, float]) -> list[str]:
    risks: list[str] = []
    if metrics["dist_ma20"] > 18 or metrics["ret20"] > 45:
        risks.append("短线过热")
    if metrics["upper_shadow_pct"] > 5 and metrics["vol_ratio_5_20"] > 1.8 and metrics["close_pos_day"] < 0.45:
        risks.append("放量长上影")
    if metrics["dist_ma20"] < -6 or metrics["dist_ma50"] < -10:
        risks.append("跌破确认支撑")
    if metrics["ret20"] < -8 and metrics["vol_ratio_5_20"] < 0.9:
        risks.append("缩量阴跌")
    if metrics.get("main_force_score", 0.0) < 0.30 and metrics["vol_ratio_5_20"] > 1.3:
        risks.append("供给未消化")
    return risks


def _hard_blocked(risks: list[str]) -> bool:
    return bool(set(risks) & {"短线过热", "放量长上影", "跌破确认支撑", "缩量阴跌"})


def _opportunity_text(lane: str, sector: str) -> str:
    text = {
        "trend_breakout": "强趋势平台突破",
        "main_force_entry": "疑似资金进场候选",
        "trend_lane_pullback": "主线趋势回踩确认",
        "sector_strength": "板块强势轮动候选",
        "wyckoff_structure": "Wyckoff结构延续候选",
    }.get(lane, lane)
    return f"{text}: {sector}" if sector else text


def _timing_text(lane: str, metrics: dict[str, float]) -> str:
    return (
        f"{lane} ret20={metrics['ret20']:.1f}% ret60={metrics['ret60']:.1f}% "
        f"MA20乖离={metrics['dist_ma20']:.1f}% 量={metrics['vol_ratio_5_20']:.2f}x"
        f" 主力={metrics.get('main_force_score', 0.0):.2f}"
    )


def _reasons(lane: str, metrics: dict[str, float], sector: str) -> list[str]:
    return [
        f"lane:{lane}",
        f"板块:{sector or '-'}",
        f"20日:{metrics['ret20']:.1f}%",
        f"60日:{metrics['ret60']:.1f}%",
        f"MA20乖离:{metrics['dist_ma20']:.1f}%",
        f"主力分:{metrics.get('main_force_score', 0.0):.2f}",
    ]


def _entry_rank(item: dict[str, Any]) -> tuple[int, float, str]:
    priority = {
        "mainline": 0,
        "main_force_entry": 1,
        "trend_lane_pullback": 2,
        "trend_breakout": 3,
        "sector_strength": 4,
        "wyckoff_structure": 5,
    }
    rank_key = _rank_key(item, priority)
    return (priority.get(rank_key, 20), -candidate_entry_score(item), str(item.get("code", "")))


def _rank_key(item: dict[str, Any], priority: dict[str, int]) -> str:
    return candidate_entry_key(item, priority.keys(), fields=("signal_key", "lane", "entry_type"))
