"""Short-horizon theme activity from stock-level daily bars."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean, median
from typing import Any

import pandas as pd

from core._price_math import clamp as _clamp
from core._price_math import sort_by_date_if_needed
from core.concept_filters import is_actionable_theme_name, is_user_facing_etf
from core.theme_radar import normalize_theme_name


@dataclass(frozen=True)
class ThemeActivityConfig:
    top_themes: int = 12
    min_members: int = 5
    min_score: float = 0.42


@dataclass(frozen=True)
class ThemeActivity:
    theme: str
    score: float
    median_ret: float
    top_ret: float
    up_ratio: float
    strong_ratio: float
    volume_ratio: float
    member_count: int
    strong_count: int
    heat_score: float
    evidence: str


def build_theme_activity_snapshot(
    *,
    trade_date: str,
    df_map: dict[str, pd.DataFrame],
    concept_map: dict[str, list[str]],
    sector_map: dict[str, str],
    concept_heat: list[dict[str, Any]] | None = None,
    theme_member_index: dict[str, list[str]] | None = None,
    config: ThemeActivityConfig | None = None,
) -> dict[str, Any]:
    cfg = config or ThemeActivityConfig()
    stock_rows = _stock_activity_rows(df_map)
    heat = _heat_by_theme(concept_heat or [])
    themes = theme_member_index if theme_member_index is not None else build_theme_member_index(concept_map, sector_map)
    rows = [_activity_for_theme(theme, members, stock_rows, heat) for theme, members in themes.items()]
    eligible = [row for row in rows if row and row.score >= cfg.min_score and row.member_count >= cfg.min_members]
    ranked = sorted(eligible, key=_activity_sort_key)[: cfg.top_themes]
    return {"trade_date": trade_date, "themes": [asdict(row) for row in ranked]}


def summarize_theme_activity(snapshot: dict[str, Any], limit: int = 6) -> str:
    themes = [row for row in (snapshot.get("themes") or []) if not is_user_facing_etf(name=str(row.get("theme") or ""))]
    themes = themes[:limit]
    if not themes:
        return "无明显全市场主题异动"
    return "；".join(
        f"{row['theme']}(中位{row['median_ret']:+.1f}%，上涨{row['up_ratio']:.0%}，强势{row['strong_count']}只)"
        for row in themes
    )


def _stock_activity_rows(df_map: dict[str, pd.DataFrame]) -> dict[str, dict[str, float]]:
    rows: dict[str, dict[str, float]] = {}
    for code, df in df_map.items():
        row = _stock_activity(df)
        if row:
            rows[str(code)] = row
    return rows


def _stock_activity(df: pd.DataFrame | None) -> dict[str, float] | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    ordered = sort_by_date_if_needed(df)
    close = pd.to_numeric(ordered["close"], errors="coerce").dropna()
    if len(close) < 2:
        return None
    prev, last = float(close.iloc[-2]), float(close.iloc[-1])
    if prev <= 0:
        return None
    return {"ret": (last / prev - 1.0) * 100.0, "volume_ratio": _latest_volume_ratio(ordered)}


def _latest_volume_ratio(df: pd.DataFrame) -> float:
    if "volume" not in df.columns:
        return 1.0
    volume = pd.to_numeric(df["volume"], errors="coerce").dropna()
    if len(volume) < 2:
        return 1.0
    base = volume.tail(21).iloc[:-1]
    avg = float(base.mean()) if not base.empty else 0.0
    return float(volume.iloc[-1]) / avg if avg > 0 else 1.0


def build_theme_member_index(concept_map: dict[str, list[str]], sector_map: dict[str, str]) -> dict[str, list[str]]:
    index: dict[str, set[str]] = {}
    for code, concepts in concept_map.items():
        _add_theme_members(index, str(code), concepts or [])
    for code, sector in sector_map.items():
        _add_theme_members(index, str(code), [sector])
    return {theme: sorted(members) for theme, members in index.items()}


def _add_theme_members(index: dict[str, set[str]], code: str, names: list[Any]) -> None:
    for name in names:
        theme = normalize_theme_name(str(name or ""))
        if theme and is_actionable_theme_name(theme):
            index.setdefault(theme, set()).add(code)


def _heat_by_theme(concept_heat: list[dict[str, Any]]) -> dict[str, float]:
    heat: dict[str, float] = {}
    for rank, item in enumerate(concept_heat, start=1):
        theme = normalize_theme_name(str(item.get("name", "")))
        if not theme or not is_actionable_theme_name(theme):
            continue
        heat[theme] = max(heat.get(theme, 0.0), _heat_score(item, rank))
    return heat


def _heat_score(item: dict[str, Any], rank: int) -> float:
    pct = _as_float(item.get("pct"))
    inflow = max(_as_float(item.get("net_inflow")), 0.0)
    if 0 < inflow < 10_000:
        inflow_yi = inflow
    else:
        inflow_yi = inflow / 100_000_000.0
    rank_score = _clamp(1.0 - (rank - 1.0) / 60.0)
    pct_score = _clamp((pct + 1.0) / 8.0)
    flow_score = _clamp(inflow_yi / 120.0)
    return 0.40 * pct_score + 0.35 * rank_score + 0.25 * flow_score


def _activity_for_theme(
    theme: str,
    members: list[str],
    stock_rows: dict[str, dict[str, float]],
    heat: dict[str, float],
) -> ThemeActivity | None:
    rows = [stock_rows[code] for code in members if code in stock_rows]
    if not rows:
        return None
    returns = [row["ret"] for row in rows]
    volumes = [row["volume_ratio"] for row in rows]
    strong_count = sum(1 for value in returns if value >= 5.0)
    up_ratio = sum(1 for value in returns if value > 0.0) / len(returns)
    strong_ratio = strong_count / len(returns)
    median_ret = float(median(returns))
    top_ret = float(mean(sorted(returns, reverse=True)[: min(10, len(returns))]))
    volume_ratio = float(median(volumes))
    heat_score = heat.get(theme, 0.0)
    score = _activity_score(median_ret, top_ret, up_ratio, strong_ratio, volume_ratio, heat_score)
    return ThemeActivity(
        theme=theme,
        score=round(score, 4),
        median_ret=round(median_ret, 2),
        top_ret=round(top_ret, 2),
        up_ratio=round(up_ratio, 4),
        strong_ratio=round(strong_ratio, 4),
        volume_ratio=round(volume_ratio, 2),
        member_count=len(rows),
        strong_count=strong_count,
        heat_score=round(heat_score, 4),
        evidence=f"成分上涨{up_ratio:.0%}，强势{strong_count}只，量比中位{volume_ratio:.2f}x",
    )


def _activity_score(
    median_ret: float,
    top_ret: float,
    up_ratio: float,
    strong_ratio: float,
    volume_ratio: float,
    heat_score: float,
) -> float:
    median_score = _clamp((median_ret + 1.0) / 6.0)
    top_score = _clamp((top_ret + 2.0) / 12.0)
    volume_score = _clamp((volume_ratio - 0.7) / 1.6)
    return _clamp(
        0.26 * median_score
        + 0.20 * top_score
        + 0.22 * up_ratio
        + 0.16 * min(strong_ratio * 3.0, 1.0)
        + 0.08 * volume_score
        + 0.08 * heat_score
    )


def _activity_sort_key(row: ThemeActivity) -> tuple[float, float, int, str]:
    return (-row.score, -row.median_ret, -row.strong_count, row.theme)


def _as_float(raw: Any) -> float:
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
