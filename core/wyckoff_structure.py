"""Dynamic trading-range diagnostics for Wyckoff signal observation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import NamedTuple

import pandas as pd

from core._price_math import swing_values
from core._price_math import to_numeric as _to_numeric
from core.wyckoff_engine import FunnelConfig, sort_by_date_if_needed


@dataclass(frozen=True)
class TradingRange:
    support: float
    resistance: float
    mid: float
    width_pct: float
    support_tests: int
    resistance_tests: int
    quality_score: float


class StructureTriggerResult(NamedTuple):
    triggers: dict[str, list[tuple[str, float]]]
    trading_ranges: dict[str, TradingRange]
    stage_map: dict[str, str]


class _StructureSeries(NamedTuple):
    high: pd.Series
    low: pd.Series
    close: pd.Series
    volume: pd.Series
    pct_chg: pd.Series


class _LastBar(NamedTuple):
    width: float
    last_close: float
    last_low: float
    prev_low: float
    last_pct: float


class _RangeCandidate(NamedTuple):
    support: float
    resistance: float
    width_pct: float
    drift_pct: float
    max_drift: float
    support_tests: int
    resistance_tests: int
    atr_pct: float


def _ensure_pct_chg(df: pd.DataFrame) -> pd.Series:
    if "pct_chg" in df.columns:
        pct = _to_numeric(df["pct_chg"])
        if not pct.isna().all():
            return pct
    return _to_numeric(df["close"]).pct_change() * 100.0


def _last_ref_volume_ratio(volume: pd.Series, window: int) -> float | None:
    if len(volume) < window + 1:
        return None
    ref = _to_numeric(volume).tail(window + 1).iloc[:-1].dropna()
    if ref.empty:
        return None
    ref_mean = float(ref.mean())
    if ref_mean <= 0:
        return None
    last = volume.iloc[-1]
    if pd.isna(last):
        return None
    return float(last) / ref_mean


def _recent_ref_volume_ratio(volume: pd.Series, recent_n: int, ref_n: int) -> float | None:
    recent_n = max(int(recent_n), 1)
    ref_n = max(int(ref_n), recent_n + 1)
    if len(volume) < ref_n + recent_n:
        return None
    recent = _to_numeric(volume).tail(recent_n).dropna()
    ref = _to_numeric(volume).tail(ref_n + recent_n).iloc[:-recent_n].dropna()
    if recent.empty or ref.empty:
        return None
    ref_max = float(ref.max())
    if ref_max <= 0:
        return None
    return float(recent.max()) / ref_max


def _range_zone(
    df: pd.DataFrame,
    *,
    lookback: int,
    min_bars: int,
    exclude_last: int,
) -> pd.DataFrame | None:
    if len(df) < min_bars + max(int(exclude_last), 0):
        return None
    df_s = sort_by_date_if_needed(df).copy()
    if exclude_last > 0:
        df_s = df_s.iloc[:-exclude_last]
    zone = df_s.tail(max(int(lookback), min_bars)).copy()
    return zone if len(zone) >= min_bars else None


def _range_boundary(
    zone: pd.DataFrame, swing_window: int
) -> tuple[pd.Series, pd.Series, pd.Series, float, float] | None:
    high = _to_numeric(zone["high"])
    low = _to_numeric(zone["low"])
    close = _to_numeric(zone["close"])
    if high.isna().all() or low.isna().all() or close.isna().all():
        return None
    swing_lows = swing_values(low, kind="low", window=swing_window)
    swing_highs = swing_values(high, kind="high", window=swing_window)
    support = float(pd.Series(swing_lows[-5:]).median()) if len(swing_lows) >= 2 else float(low.quantile(0.10))
    resistance = float(pd.Series(swing_highs[-5:]).median()) if len(swing_highs) >= 2 else float(high.quantile(0.90))
    if support <= 0 or resistance <= support:
        return None
    return high, low, close, support, resistance


def _range_width_ok(support: float, resistance: float, cfg: FunnelConfig) -> float | None:
    width_pct = (resistance - support) / support * 100.0
    max_width = min(max(float(getattr(cfg, "spring_tr_max_range_pct", 30.0)) * 1.5, 24.0), 55.0)
    if width_pct < 4.0 or width_pct > max_width:
        return None
    return float(width_pct)


def _range_drift(close: pd.Series, cfg: FunnelConfig) -> tuple[float, float] | None:
    clean = close.dropna()
    first_close = clean.iloc[0]
    last_close = clean.iloc[-1]
    if first_close <= 0:
        return None
    drift_pct = abs((float(last_close) - float(first_close)) / float(first_close) * 100.0)
    max_drift = max(float(getattr(cfg, "spring_tr_max_drift_pct", 12.0)) * 1.5, 18.0)
    return (float(drift_pct), float(max_drift)) if drift_pct <= max_drift else None


def _range_tests(high: pd.Series, low: pd.Series, support: float, resistance: float) -> tuple[int, int] | None:
    tolerance = 0.035
    support_tests = int((low <= support * (1.0 + tolerance)).sum())
    resistance_tests = int((high >= resistance * (1.0 - tolerance)).sum())
    if support_tests < 2 or resistance_tests < 2:
        return None
    return support_tests, resistance_tests


def _range_atr_pct(zone: pd.DataFrame, window: int) -> float:
    high = _to_numeric(zone["high"])
    low = _to_numeric(zone["low"])
    close = _to_numeric(zone["close"])
    true_range = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(
        axis=1
    )
    atr = true_range.tail(max(int(window), 1)).mean()
    last_close = close.iloc[-1]
    return float(atr / last_close * 100.0) if pd.notna(atr) and pd.notna(last_close) and last_close > 0 else 0.0


def _range_quality(
    width_pct: float,
    drift_pct: float,
    max_drift: float,
    support_tests: int,
    resistance_tests: int,
    atr_pct: float,
) -> float:
    ideal_width = min(max(atr_pct * 4.0, 6.0), 40.0)
    width_score = max(0.0, 1.0 - abs(width_pct - ideal_width) / max(ideal_width, 1.0))
    test_score = min((support_tests + resistance_tests) / 6.0, 1.0)
    drift_score = max(0.0, 1.0 - drift_pct / max_drift)
    return float(0.45 * test_score + 0.35 * width_score + 0.20 * drift_score)


def _range_candidate(
    df: pd.DataFrame, cfg: FunnelConfig, lookback: int, swing_window: int, exclude_last: int, min_bars: int
):
    zone = _range_zone(df, lookback=lookback, min_bars=min_bars, exclude_last=exclude_last)
    if zone is None:
        return None
    boundary = _range_boundary(zone, swing_window)
    if boundary is None:
        return None
    high, low, close, support, resistance = boundary
    width_pct = _range_width_ok(support, resistance, cfg)
    drift = _range_drift(close, cfg)
    tests = _range_tests(high, low, support, resistance)
    if width_pct is None or drift is None or tests is None:
        return None
    drift_pct, max_drift = drift
    support_tests, resistance_tests = tests
    return _RangeCandidate(
        support=support,
        resistance=resistance,
        width_pct=width_pct,
        drift_pct=drift_pct,
        max_drift=max_drift,
        support_tests=support_tests,
        resistance_tests=resistance_tests,
        atr_pct=_range_atr_pct(zone, getattr(cfg, "spring_tr_atr_window", 20)),
    )


def identify_trading_range(
    df: pd.DataFrame,
    cfg: FunnelConfig | None = None,
    *,
    lookback: int = 90,
    swing_window: int = 3,
    exclude_last: int = 1,
) -> TradingRange | None:
    """Identify a recent Wyckoff trading range from swing highs/lows.

    `exclude_last=1` is the default because trigger detection should compare
    today's bar against the range that was visible before today's close.
    """

    if df is None or df.empty:
        return None
    cfg = cfg or FunnelConfig()
    min_bars = max(40, swing_window * 2 + 20)
    candidate = _range_candidate(df, cfg, lookback, swing_window, exclude_last, min_bars)
    if candidate is None:
        return None

    quality_score = _range_quality(
        candidate.width_pct,
        candidate.drift_pct,
        candidate.max_drift,
        candidate.support_tests,
        candidate.resistance_tests,
        candidate.atr_pct,
    )
    mid = candidate.support + (candidate.resistance - candidate.support) / 2.0
    return TradingRange(
        support=candidate.support,
        resistance=candidate.resistance,
        mid=mid,
        width_pct=candidate.width_pct,
        support_tests=candidate.support_tests,
        resistance_tests=candidate.resistance_tests,
        quality_score=quality_score,
    )


def _infer_stage(df: pd.DataFrame, tr: TradingRange, trigger_keys: set[str]) -> str:
    close = _to_numeric(df["close"])
    last_close = float(close.iloc[-1])
    width = tr.resistance - tr.support
    if last_close >= tr.resistance:
        return "Markup"
    if "spring" in trigger_keys:
        return "Accum_C"
    if "lps" in trigger_keys:
        return "Accum_C"
    if last_close <= tr.support + width * 0.35:
        return "Accum_B"
    return "Accum_A"


def _empty_structure_triggers() -> dict[str, list[tuple[str, float]]]:
    return {"sos": [], "spring": [], "lps": [], "evr": []}


def _structure_series(df: pd.DataFrame) -> _StructureSeries | None:
    series = _StructureSeries(
        high=_to_numeric(df["high"]),
        low=_to_numeric(df["low"]),
        close=_to_numeric(df["close"]),
        volume=_to_numeric(df["volume"]),
        pct_chg=_ensure_pct_chg(df),
    )
    if series.close.isna().all() or series.low.isna().all() or series.high.isna().all() or series.volume.isna().all():
        return None
    return series


def _last_bar(series: _StructureSeries, tr: TradingRange) -> _LastBar:
    last_pct = float(series.pct_chg.iloc[-1]) if pd.notna(series.pct_chg.iloc[-1]) else 0.0
    return _LastBar(
        width=tr.resistance - tr.support,
        last_close=float(series.close.iloc[-1]),
        last_low=float(series.low.iloc[-1]),
        prev_low=float(series.low.iloc[-2]) if len(series.low) >= 2 else float(series.low.iloc[-1]),
        last_pct=last_pct,
    )


def _sos_trigger_score(series: _StructureSeries, bar: _LastBar, tr: TradingRange, cfg: FunnelConfig) -> float | None:
    vol_ratio = _last_ref_volume_ratio(series.volume, max(int(cfg.sos_vol_window), 5))
    if vol_ratio is None:
        return None
    breakout_tolerance = float(getattr(cfg, "sos_breakout_tolerance", 0.01))
    structure_breakout = bar.last_close >= tr.resistance * (1.0 - breakout_tolerance)
    enough_push = bar.last_pct >= float(getattr(cfg, "sos_pct_min", 6.0))
    enough_volume = vol_ratio >= float(getattr(cfg, "sos_vol_ratio", 2.0))
    if not (structure_breakout and enough_push and enough_volume):
        return None
    score = vol_ratio + max((bar.last_close - tr.resistance) / tr.resistance * 100.0, 0.0)
    return float(score + tr.quality_score)


def _spring_trigger_score(series: _StructureSeries, bar: _LastBar, tr: TradingRange, cfg: FunnelConfig) -> float | None:
    vol_ratio = _last_ref_volume_ratio(series.volume, 5)
    pierced = min(bar.prev_low, bar.last_low) <= tr.support * 0.995
    recovered = bar.last_close > tr.support * 1.005
    still_in_range = bar.last_close < tr.mid + bar.width * 0.25
    enough_volume = vol_ratio is not None and vol_ratio >= float(getattr(cfg, "spring_vol_ratio", 1.1))
    if not (pierced and recovered and still_in_range and enough_volume):
        return None
    recovery = (bar.last_close - tr.support) / tr.support * 100.0
    return float(recovery + tr.quality_score)


def _lps_trigger_score(series: _StructureSeries, bar: _LastBar, tr: TradingRange, cfg: FunnelConfig) -> float | None:
    lookback = max(int(getattr(cfg, "lps_lookback", 3)), 1)
    dry_ratio = _recent_ref_volume_ratio(series.volume, lookback, max(int(getattr(cfg, "lps_vol_ref_window", 20)), 10))
    recent_lows = series.low.tail(lookback)
    near_support = float(recent_lows.min()) <= tr.support + bar.width * 0.35
    holds_support = bar.last_close > tr.support
    if dry_ratio is None or not near_support or not holds_support or dry_ratio > float(cfg.lps_vol_dry_ratio):
        return None
    return float((1.0 - dry_ratio) + tr.quality_score)


def _evr_trigger_score(series: _StructureSeries, bar: _LastBar, tr: TradingRange, cfg: FunnelConfig) -> float | None:
    vol_ratio = _last_ref_volume_ratio(series.volume, max(int(cfg.evr_vol_window), 10))
    if (
        not getattr(cfg, "enable_evr_trigger", False)
        or vol_ratio is None
        or vol_ratio < float(cfg.evr_vol_ratio)
        or bar.last_close > tr.mid
        or not (-float(cfg.evr_max_drop) <= bar.last_pct <= float(cfg.evr_max_rise))
        or bar.last_close < tr.support * 0.98
    ):
        return None
    return float(vol_ratio + tr.quality_score)


def _append_structure_hits(
    sym: str,
    triggers: dict[str, list[tuple[str, float]]],
    series: _StructureSeries,
    bar: _LastBar,
    tr: TradingRange,
    cfg: FunnelConfig,
) -> set[str]:
    hit_keys: set[str] = set()
    for key, score in (
        ("sos", _sos_trigger_score(series, bar, tr, cfg)),
        ("spring", _spring_trigger_score(series, bar, tr, cfg)),
        ("lps", _lps_trigger_score(series, bar, tr, cfg)),
        ("evr", _evr_trigger_score(series, bar, tr, cfg)),
    ):
        if score is not None:
            triggers[key].append((sym, score))
            hit_keys.add(key)
    return hit_keys


def detect_structure_triggers(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: FunnelConfig,
    *,
    lookback: int = 90,
) -> StructureTriggerResult:
    """Run dynamic-TR Spring / SOS / LPS / EVR detection."""

    triggers = _empty_structure_triggers()
    ranges: dict[str, TradingRange] = {}
    stage_map: dict[str, str] = {}

    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty or len(df) < 60:
            continue
        df_s = sort_by_date_if_needed(df).copy()
        tr = identify_trading_range(df_s, cfg, lookback=lookback, exclude_last=1)
        if tr is None:
            continue

        series = _structure_series(df_s)
        if series is None:
            continue

        ranges[sym] = tr
        hit_keys = _append_structure_hits(sym, triggers, series, _last_bar(series, tr), tr, cfg)
        stage_map[sym] = _infer_stage(df_s, tr, hit_keys)

    return StructureTriggerResult(triggers=triggers, trading_ranges=ranges, stage_map=stage_map)


def build_structure_shadow(
    legacy_triggers: dict[str, list[tuple[str, float]]],
    structure_result: StructureTriggerResult,
    *,
    universe_count: int,
) -> dict:
    """Compare structure-aware signals with formal Layer 4 without promoting them."""
    comparisons = {}
    for signal in ("spring", "lps", "sos", "evr"):
        legacy_scores = {str(symbol): float(score) for symbol, score in legacy_triggers.get(signal, [])}
        structure_scores = {str(symbol): float(score) for symbol, score in structure_result.triggers.get(signal, [])}
        legacy = set(legacy_scores)
        structure = set(structure_scores)
        comparisons[signal] = {
            "legacy_count": len(legacy),
            "structure_count": len(structure),
            "both_count": len(legacy & structure),
            "legacy_only_count": len(legacy - structure),
            "structure_only_count": len(structure - legacy),
            "both": sorted(legacy & structure),
            "legacy_only": sorted(legacy - structure),
            "structure_only": sorted(structure - legacy),
            "structure_scores": structure_scores,
        }
    stage_counts: dict[str, int] = {}
    for stage in structure_result.stage_map.values():
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    range_count = len(structure_result.trading_ranges)
    return {
        "mode": "observation_only",
        "status": "ok",
        "affects_formal_selection": False,
        "universe_count": max(int(universe_count), 0),
        "range_identified_count": range_count,
        "range_coverage": round(range_count / universe_count, 4) if universe_count > 0 else 0.0,
        "trading_ranges": {symbol: asdict(value) for symbol, value in structure_result.trading_ranges.items()},
        "diagnostic_stage_map": structure_result.stage_map,
        "diagnostic_stage_counts": stage_counts,
        "by_trigger": comparisons,
    }


__all__ = [
    "StructureTriggerResult",
    "TradingRange",
    "build_structure_shadow",
    "detect_structure_triggers",
    "identify_trading_range",
]
