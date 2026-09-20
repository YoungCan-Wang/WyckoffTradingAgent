"""Shared price/volume math helpers used across candidate and mainline scoring modules.

These were duplicated verbatim in 5-7 files; centralized here so each formula has one
implementation to keep correct.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)
DATE_SORTED_ATTR = "_wyckoff_date_sorted"


def sort_by_date_if_needed(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "date" not in df.columns:
        return df
    if df.attrs.get(DATE_SORTED_ATTR) is True:
        return df
    try:
        if df["date"].is_monotonic_increasing:
            return df
    except Exception:
        logger.debug("Monotonic check failed, falling back to sort", exc_info=True)
    return df.sort_values("date")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def range_pos(value: float, low: float, high: float) -> float:
    """Position of *value* in [low, high]; returns 0.5 when range is empty."""
    return 0.5 if high <= low else clamp((value - low) / (high - low))


def to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def swing_values(series: pd.Series, *, kind: str, window: int) -> list[float]:
    values = to_numeric(series).reset_index(drop=True)
    width = max(int(window), 1)
    if kind not in {"low", "high"} or len(values) < width * 2 + 1:
        return []
    swings: list[float] = []
    for index in range(width, len(values) - width):
        current = values.iloc[index]
        if pd.isna(current):
            continue
        span = values.iloc[index - width : index + width + 1].dropna()
        boundary = span.min() if kind == "low" else span.max()
        if not span.empty and float(current) == float(boundary):
            swings.append(float(current))
    return swings


def numeric_column(df: pd.DataFrame, column: str, *, dropna: bool = True) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    series = pd.to_numeric(df[column], errors="coerce")
    return series.dropna() if dropna else series


def ret_pct(close: pd.Series, lookback: int) -> float:
    if len(close) <= lookback:
        return 0.0
    start = float(close.iloc[-lookback - 1])
    return 0.0 if start <= 0 else (float(close.iloc[-1]) / start - 1.0) * 100.0


def dist_pct(value: float, base: float) -> float:
    return 0.0 if base <= 0 else (float(value) / float(base) - 1.0) * 100.0


def drawdown_pct(close: pd.Series, lookback: int) -> float:
    recent = close.tail(max(lookback, 1))
    if recent.empty:
        return 0.0
    high = float(recent.max())
    return 0.0 if high <= 0 else (float(recent.iloc[-1]) / high - 1.0) * -100.0


def upper_shadow_pct(df: pd.DataFrame, open_: pd.Series, high: pd.Series, close: pd.Series) -> float:
    if high.empty or close.empty:
        return 0.0
    base = float(close.iloc[-1])
    body_top = max(base, float(open_.iloc[-1]) if not open_.empty else base)
    return 0.0 if base <= 0 else max(float(high.iloc[-1]) - body_top, 0.0) / base * 100.0


def day_close_pos(close: pd.Series, high: pd.Series, low: pd.Series, *, use_tail: bool = False) -> float:
    if high.empty or low.empty:
        return 0.5
    last_close = float(close.iloc[-1])
    if use_tail:
        lo = float(low.tail(1).min()) if not low.empty else float(close.tail(1).min())
        hi = float(high.tail(1).max()) if not high.empty else float(close.tail(1).max())
    else:
        lo = float(low.iloc[-1])
        hi = float(high.iloc[-1])
    return range_pos(last_close, lo, hi)


def vol_ratio(volume: pd.Series) -> float:
    if len(volume) < 20:
        return 1.0
    base = float(volume.tail(20).mean())
    return 1.0 if base <= 0 else float(volume.tail(5).mean()) / base


def fib_retracement_levels(
    swing_high: float,
    swing_low: float,
    levels: tuple[float, ...] = (0.382, 0.500, 0.618),
) -> dict[float, float]:
    """Calculate price retracement levels from swing high towards swing low."""
    if swing_high <= swing_low or swing_low <= 0:
        return {}
    span = swing_high - swing_low
    return {lvl: swing_high - span * lvl for lvl in levels}


def fib_absorption_check(
    curr_low: float,
    curr_close: float,
    price_level: float,
    curr_vol: float,
    avg_vol: float,
    *,
    min_vol_ratio: float = 1.0,
) -> tuple[bool, bool]:
    """Check if price touched fib level, closed above it, and showed volume absorption."""
    if price_level <= 0 or curr_close <= 0:
        return False, False
    touched_and_held = bool(curr_low <= price_level and curr_close >= price_level)
    vol_ratio_val = (curr_vol / avg_vol) if avg_vol > 0 else 1.0
    absorption = bool(touched_and_held and vol_ratio_val >= min_vol_ratio)
    return touched_and_held, absorption
