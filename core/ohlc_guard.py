"""Reject structurally impossible OHLCV bars at the ingestion boundary."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd

from utils.env import env_bool

_OHLC = ("open", "high", "low", "close")


def dirty_bar_guard_enabled() -> bool:
    return env_bool("OHLCV_DIRTY_BAR_GUARD", True)


def dirty_bar_reason(row: pd.Series) -> str | None:
    flags = _dirty_flags(pd.DataFrame([row]))
    hit = flags.loc[0]
    for name in ("nan_ohlc", "non_positive_price", "high_lt_low", "high_lt_body", "low_gt_body", "negative_volume"):
        if bool(hit[name]):
            return name
    return None


def sanitize_ohlcv_frame(df: pd.DataFrame | None, *, drop: bool = True) -> tuple[pd.DataFrame, dict[str, int]]:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df, {}
    if not set(_OHLC).issubset(df.columns):
        return df, {}
    flags = _dirty_flags(df)
    dirty = flags.any(axis=1)
    reasons = {name: int(flags[name].sum()) for name in flags.columns if bool(flags[name].any())}
    if drop:
        return df.loc[~dirty].reset_index(drop=True), reasons
    return _nullify_dirty_bars(df, dirty), reasons


def _nullify_dirty_bars(df: pd.DataFrame, dirty: pd.Series) -> pd.DataFrame:
    """分析页要留日期行；漏斗批量路径才丢 bar。"""
    if not dirty.any():
        return df
    out = df.copy()
    for col in _OHLC:
        out.loc[dirty, col] = pd.NA
    if "volume" in out.columns:
        out.loc[dirty, "volume"] = pd.NA
    return out


def sanitize_ohlcv_map(
    df_map: dict[str, pd.DataFrame],
    stats: dict[str, Any] | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    out_stats = dict(stats or {})
    if not dirty_bar_guard_enabled():
        out_stats["dirty_bar_guard"] = "off"
        return df_map, out_stats
    cleaned, dropped_bars, dropped_symbols, reason_total = _sanitize_map_body(df_map)
    out_stats.update(
        {
            "dirty_bar_guard": "on",
            "dirty_bars_dropped": dropped_bars,
            "dirty_symbols_dropped": dropped_symbols,
            "dirty_bar_reasons": dict(reason_total),
        }
    )
    return cleaned, out_stats


def _sanitize_map_body(
    df_map: dict[str, pd.DataFrame],
) -> tuple[dict[str, pd.DataFrame], int, int, Counter[str]]:
    cleaned: dict[str, pd.DataFrame] = {}
    dropped_symbols = 0
    reason_total: Counter[str] = Counter()
    dropped_bars = 0
    for symbol, frame in df_map.items():
        tip_before = _frame_tip_date(frame)
        kept, reasons = sanitize_ohlcv_frame(frame)
        reason_total.update(reasons)
        dropped_bars += max(len(frame) - len(kept), 0) if frame is not None else 0
        if kept.empty:
            dropped_symbols += 1
            continue
        # 丢掉最新一根脏 K 后若仍留昨日序列，漏斗会把昨收当今日参与打分/RPS。
        if tip_before is not None and _frame_tip_date(kept) != tip_before:
            dropped_symbols += 1
            reason_total["stale_tip_after_dirty_drop"] += 1
            continue
        cleaned[symbol] = kept
    return cleaned, dropped_bars, dropped_symbols, reason_total


def _frame_tip_date(frame: pd.DataFrame | None) -> object | None:
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    return dates.max().date() if not dates.empty else None


def _dirty_flags(df: pd.DataFrame) -> pd.DataFrame:
    open_ = pd.to_numeric(df.get("open"), errors="coerce")
    high = pd.to_numeric(df.get("high"), errors="coerce")
    low = pd.to_numeric(df.get("low"), errors="coerce")
    close = pd.to_numeric(df.get("close"), errors="coerce")
    volume = pd.to_numeric(df.get("volume"), errors="coerce") if "volume" in df.columns else None
    nan_ohlc = open_.isna() | high.isna() | low.isna() | close.isna()
    return pd.DataFrame(
        {
            "nan_ohlc": nan_ohlc,
            "non_positive_price": (close <= 0) | (high <= 0) | (low <= 0) | (open_ <= 0),
            "high_lt_low": high < low,
            "high_lt_body": high < pd.concat([open_, close], axis=1).max(axis=1),
            "low_gt_body": low > pd.concat([open_, close], axis=1).min(axis=1),
            "negative_volume": volume.lt(0) if volume is not None else False,
        },
        index=df.index,
    )
