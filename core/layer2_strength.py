"""Shared calculations for Layer 2 strength screening."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BenchmarkContext:
    sorted_df: pd.DataFrame | None
    latest_date: object | None
    dropping: bool
    pct_by_date: dict[object, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RpsContext:
    fast: dict[str, float]
    slow: dict[str, float]
    active: bool


@dataclass(frozen=True)
class RsSnapshot:
    rs_long: float | None
    rs_short: float | None
    bench_long_ret: float | None
    bench_short_ret: float | None


@dataclass(frozen=True)
class Layer2SymbolState:
    close: pd.Series
    last_close: float
    last_ma_short: float
    last_ma_long: float
    bullish_alignment: bool
    holding_ma20: bool


@dataclass(frozen=True)
class Layer2RpsState:
    fast: float | None
    slow: float | None
    momentum_ok: bool
    ambush_ok: bool


@dataclass(frozen=True)
class Layer2SymbolResult:
    passed: bool
    channel: str
    pre_ignition: bool
    channels: dict[str, bool]


def close_return_pct(close_series: pd.Series, lookback: int) -> float | None:
    close = pd.to_numeric(close_series, errors="coerce").dropna()
    lb = max(int(lookback), 1)
    if len(close) <= lb:
        return None
    start = float(close.iloc[-lb - 1])
    end = float(close.iloc[-1])
    return None if start == 0 else (end - start) / start * 100.0


def build_benchmark_context(
    bench_df: pd.DataFrame | None,
    cfg: Any,
    *,
    sort_frame: Callable[[pd.DataFrame], pd.DataFrame],
    latest_trade_date: Callable[[pd.DataFrame], object | None],
) -> BenchmarkContext:
    if bench_df is None or bench_df.empty:
        return BenchmarkContext(None, None, False)
    bench_sorted = sort_frame(bench_df)
    latest_date = latest_trade_date(bench_sorted)
    dropping = _benchmark_dropping(bench_sorted, int(cfg.bench_drop_days), float(cfg.bench_drop_threshold))
    return BenchmarkContext(bench_sorted, latest_date, dropping, _benchmark_pct_lookup(bench_sorted))


def build_rps_context(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: Any,
    *,
    rps_universe: list[str] | None,
    sort_frame: Callable[[pd.DataFrame], pd.DataFrame],
) -> RpsContext:
    if not cfg.enable_rps_filter:
        return RpsContext({}, {}, False)
    rows = _rps_rows(rps_universe if rps_universe else symbols, df_map, cfg, sort_frame)
    if not rows:
        return RpsContext({}, {}, False)
    rps_df = pd.DataFrame(rows, columns=["sym", "ret_fast", "ret_slow"])
    rps_df["rps_fast"] = rps_df["ret_fast"].rank(pct=True, ascending=True, method="average") * 100.0
    rps_df["rps_slow"] = rps_df["ret_slow"].rank(pct=True, ascending=True, method="average") * 100.0
    return RpsContext(
        rps_df.set_index("sym")["rps_fast"].astype(float).to_dict(),
        rps_df.set_index("sym")["rps_slow"].astype(float).to_dict(),
        True,
    )


def calc_relative_strength(
    stock_df: pd.DataFrame,
    bench_sorted_df: pd.DataFrame,
    cfg: Any,
    bench_pct_by_date: dict[object, object] | None = None,
) -> RsSnapshot:
    lookup = bench_pct_by_date if bench_pct_by_date is not None else _benchmark_pct_lookup(bench_sorted_df)
    stock_pct, bench_pct = _aligned_pct_series(stock_df, lookup)
    w_long = max(int(cfg.rs_window_long), 1)
    w_short = max(int(cfg.rs_window_short), 1)
    if len(stock_pct) < max(w_long, w_short):
        return RsSnapshot(None, None, None, None)
    s_long = _cum_return_pct_from_series(stock_pct.tail(w_long))
    b_long = _cum_return_pct_from_series(bench_pct.tail(w_long))
    s_short = _cum_return_pct_from_series(stock_pct.tail(w_short))
    b_short = _cum_return_pct_from_series(bench_pct.tail(w_short))
    if s_long is None or b_long is None or s_short is None or b_short is None:
        return RsSnapshot(None, None, None, None)
    return RsSnapshot(s_long - b_long, s_short - b_short, b_long, b_short)


def _benchmark_pct_lookup(bench_df: pd.DataFrame) -> dict[object, object]:
    if not {"date", "pct_chg"}.issubset(bench_df.columns) or bench_df["date"].duplicated().any():
        return {}
    return dict(zip(bench_df["date"], bench_df["pct_chg"], strict=True))


def _aligned_pct_series(stock_df: pd.DataFrame, bench_pct_by_date: dict[object, object]) -> tuple[pd.Series, pd.Series]:
    if not bench_pct_by_date or not {"date", "pct_chg"}.issubset(stock_df.columns):
        return pd.Series(dtype=float), pd.Series(dtype=float)
    pairs = [
        (pct, bench_pct_by_date[day])
        for day, pct in zip(stock_df["date"], stock_df["pct_chg"], strict=True)
        if day in bench_pct_by_date
    ]
    if not pairs:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    stock_pct, bench_pct = zip(*pairs, strict=True)
    return pd.Series(stock_pct), pd.Series(bench_pct)


def evaluate_layer2_symbol(
    sym: str,
    df_sorted: pd.DataFrame,
    cfg: Any,
    *,
    bench_ctx: BenchmarkContext,
    rps_ctx: RpsContext,
    detect_sos: Callable[[pd.DataFrame, Any], float | None],
) -> Layer2SymbolResult:
    state = _symbol_state(df_sorted, cfg, bench_ctx)
    rps_state = _rps_state(sym, state.close, df_sorted, cfg, rps_ctx)
    momentum_rs_ok, ambush_rs_ok = _rs_flags(df_sorted, state, cfg, bench_ctx, rps_state.slow)
    channels = _layer2_channels(
        df_sorted, state, cfg, bench_ctx, rps_ctx, rps_state, momentum_rs_ok, ambush_rs_ok, detect_sos
    )
    if any(channels.values()):
        return Layer2SymbolResult(True, "+".join(channel_labels(channels)), False, channels)
    pre_ignition = cfg.enable_pre_ignition_watch and pre_ignition_ok(
        cfg=cfg,
        close=state.close,
        df_sorted=df_sorted,
        last_ma_long=state.last_ma_long,
        last_close=state.last_close,
        bullish_alignment=state.bullish_alignment,
        holding_ma20=state.holding_ma20,
        rps_slow=rps_state.slow,
    )
    return Layer2SymbolResult(False, "", pre_ignition, channels)


def rps_slope_state(close_series: pd.Series, cfg: Any, *, active: bool, row_count: int) -> tuple[bool, float]:
    if not cfg.enable_rps_filter or not active or row_count < cfg.rps_slope_window:
        return True, 0.0
    close = pd.to_numeric(close_series, errors="coerce")
    recent = [float(close.iloc[i]) for i in range(-max(int(cfg.rps_slope_window), 2), 0) if len(close) + i >= 0]
    if len(recent) < 2 or recent[0] <= 0:
        return True, 0.0
    cum_returns = [(price - recent[0]) / recent[0] * 100.0 for price in recent]
    slope = float(np.polyfit(np.arange(len(cum_returns)), np.array(cum_returns), 1)[0])
    return slope >= cfg.rps_slope_min, slope


def rps_filter_flags(
    cfg: Any,
    *,
    active: bool,
    rps_fast: float | None,
    rps_slow: float | None,
    slope_ok: bool,
    slope_value: float,
) -> tuple[bool, bool]:
    if not cfg.enable_rps_filter or not active:
        return True, True
    has_rps = rps_fast is not None and rps_slow is not None
    accel_bypass = has_rps and _rps_accel_bypass(cfg, rps_fast, rps_slow, slope_value)
    momentum_ok = has_rps and (
        (rps_fast >= cfg.rps_fast_min and rps_slow >= cfg.rps_slow_min and slope_ok)
        or (rps_slow >= cfg.rps_slow_strong_bypass and rps_fast >= cfg.rps_fast_bypass_min)
        or accel_bypass
    )
    ambush_ok = has_rps and rps_fast <= cfg.ambush_rps_fast_max and rps_slow >= cfg.ambush_rps_slow_min
    return momentum_ok, ambush_ok


def channel_labels(channels: dict[str, bool]) -> list[str]:
    labels = [
        label
        for key, label in (
            ("momentum", "主升通道"),
            ("ambush", "潜伏通道"),
            ("accum", "吸筹通道"),
            ("dry_vol", "地量蓄势"),
            ("rs_div", "暗中护盘"),
            ("trend_cont", "趋势延续"),
            ("breakout_accel", "加速突破"),
            ("sos", "点火破局"),
        )
        if channels.get(key)
    ]
    return labels


def pre_ignition_ok(
    *,
    cfg: Any,
    close: pd.Series,
    df_sorted: pd.DataFrame,
    last_ma_long: float,
    last_close: float,
    bullish_alignment: bool,
    holding_ma20: bool,
    rps_slow: float | None,
) -> bool:
    if pd.isna(last_ma_long) or float(last_ma_long) <= 0 or pd.isna(last_close):
        return False
    bias = (float(last_close) - float(last_ma_long)) / float(last_ma_long)
    has_structure = (bullish_alignment or holding_ma20) and bias <= cfg.pre_ignition_bias_max
    has_rps = rps_slow is not None and rps_slow >= cfg.pre_ignition_rps_slow_min
    return (
        has_structure
        and has_rps
        and _pre_ignition_volume_ok(df_sorted, int(cfg.sos_vol_window), float(cfg.pre_ignition_vol_ratio_min))
    )


def ambush_channel_ok(
    cfg: Any,
    *,
    close: pd.Series,
    last_close: float,
    last_ma_long: float,
    rs_ok: bool,
    rps_ok: bool,
) -> bool:
    if not cfg.enable_ambush_channel or pd.isna(last_ma_long) or float(last_ma_long) <= 0 or pd.isna(last_close):
        return False
    bias_200 = (float(last_close) - float(last_ma_long)) / float(last_ma_long)
    ret20 = close_return_pct(close, 20)
    shape_ok = abs(bias_200) <= cfg.ambush_bias_200_abs_max and ret20 is not None and ret20 <= cfg.ambush_ret20_max
    return shape_ok and rs_ok and rps_ok


def accumulation_channel_ok(
    cfg: Any,
    *,
    df_sorted: pd.DataFrame,
    close: pd.Series,
    last_close: float,
    last_ma_short: float,
    last_ma_long: float,
) -> bool:
    if not cfg.enable_accumulation_channel:
        return False
    if len(df_sorted) < max(cfg.accum_lookback_days, cfg.accum_vol_dry_ref_window):
        return False
    low_ok = _low_position_ok(close, last_close, int(cfg.accum_lookback_days), float(cfg.accum_price_from_low_max))
    range_ok = low_ok and _range_contract_ok(df_sorted, int(cfg.accum_range_window), float(cfg.accum_range_max_pct))
    vol_ok = range_ok and _volume_dry_ok(
        df_sorted,
        int(cfg.accum_vol_dry_window),
        int(cfg.accum_vol_dry_ref_window),
        float(cfg.accum_vol_dry_ratio),
    )
    return vol_ok and _ma_gap_ok(last_ma_short, last_ma_long, float(cfg.accum_ma_gap_max))


def dry_volume_channel_ok(cfg: Any, *, df_sorted: pd.DataFrame, close: pd.Series, last_close: float) -> bool:
    if not cfg.enable_dry_vol_channel or len(df_sorted) < cfg.dry_vol_ref_window:
        return False
    if not _low_position_ok(close, last_close, int(cfg.dry_vol_ref_window), float(cfg.dry_vol_price_from_low_max)):
        return False
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce")
    ref_vol = vol.tail(max(int(cfg.dry_vol_ref_window), 2))
    if len(ref_vol.dropna()) < 50:
        return False
    vol_threshold = float(np.quantile(ref_vol.dropna().values, cfg.dry_vol_quantile))
    return float(vol.tail(cfg.dry_vol_lookback).min()) <= vol_threshold


def rs_divergence_channel_ok(
    cfg: Any,
    *,
    df_sorted: pd.DataFrame,
    bench_sorted: pd.DataFrame | None,
    close: pd.Series,
    last_close: float,
) -> bool:
    if not _rs_divergence_base_ok(cfg, df_sorted, bench_sorted):
        return False
    bench_close = pd.to_numeric(bench_sorted.get("close"), errors="coerce")
    if len(bench_close.dropna()) < cfg.rs_div_bench_ref_window:
        return False
    low_ok = _low_position_ok(
        close, last_close, max(int(cfg.dry_vol_ref_window), 250), float(cfg.rs_div_price_from_low_max)
    )
    return (
        low_ok
        and _bench_made_lower_low(bench_close, cfg)
        and _stock_higher_low_with_volume(df_sorted, bench_sorted, cfg)
    )


def breakout_accel_channel_ok(
    cfg: Any,
    *,
    df_sorted: pd.DataFrame,
    close: pd.Series,
    last_close: float,
    last_ma_short: float,
    bullish_alignment: bool,
    rps_fast: float | None,
    active: bool,
) -> bool:
    if not cfg.enable_breakout_accel_channel or not active:
        return False
    above_ma50 = pd.notna(last_ma_short) and float(last_close) > float(last_ma_short)
    rps_ok = rps_fast is not None and rps_fast >= cfg.breakout_accel_rps_fast_min
    if not above_ma50 or bullish_alignment or not rps_ok:
        return False
    ret = close_return_pct(close, cfg.breakout_accel_ret_window)
    return ret is not None and ret >= cfg.breakout_accel_ret_min and _breakout_volume_ok(df_sorted, cfg)


def trend_continuation_channel_ok(
    cfg: Any,
    *,
    df_sorted: pd.DataFrame,
    close: pd.Series,
    bullish_alignment: bool,
    rps_slow: float | None,
    active: bool,
) -> bool:
    if not cfg.enable_trend_cont_channel or not bullish_alignment or not active:
        return False
    if rps_slow is None or rps_slow < cfg.trend_cont_rps_slow_min:
        return False
    drawdown_ok = _trend_drawdown_ok(close, int(cfg.trend_cont_drawdown_window), float(cfg.trend_cont_max_drawdown_pct))
    return drawdown_ok and _trend_volume_ok(df_sorted, float(cfg.trend_cont_vol_ratio_min))


def _symbol_state(df_sorted: pd.DataFrame, cfg: Any, bench_ctx: BenchmarkContext) -> Layer2SymbolState:
    close = df_sorted["close"].astype(float)
    ma_short = close.rolling(cfg.ma_short).mean()
    ma_long = close.rolling(cfg.ma_long).mean()
    last_ma_short = ma_short.iloc[-1]
    last_ma_long = ma_long.iloc[-1]
    last_close = close.iloc[-1]
    bullish_alignment = pd.notna(last_ma_short) and pd.notna(last_ma_long) and last_ma_short > last_ma_long
    holding_ma20 = _holding_ma20(close, cfg, bench_ctx.dropping, last_close)
    return Layer2SymbolState(close, last_close, last_ma_short, last_ma_long, bullish_alignment, holding_ma20)


def _holding_ma20(close: pd.Series, cfg: Any, bench_dropping: bool, last_close: float) -> bool:
    if not bench_dropping:
        return False
    last_ma_hold = close.rolling(cfg.ma_hold).mean().iloc[-1]
    return pd.notna(last_ma_hold) and last_close >= last_ma_hold


def _rps_state(sym: str, close: pd.Series, df_sorted: pd.DataFrame, cfg: Any, rps_ctx: RpsContext) -> Layer2RpsState:
    rps_fast = rps_ctx.fast.get(sym)
    rps_slow = rps_ctx.slow.get(sym)
    slope_ok, slope_value = rps_slope_state(close, cfg, active=rps_ctx.active, row_count=len(df_sorted))
    momentum_ok, ambush_ok = rps_filter_flags(
        cfg,
        active=rps_ctx.active,
        rps_fast=rps_fast,
        rps_slow=rps_slow,
        slope_ok=slope_ok,
        slope_value=slope_value,
    )
    return Layer2RpsState(rps_fast, rps_slow, momentum_ok, ambush_ok)


def _rs_flags(
    df_sorted: pd.DataFrame,
    state: Layer2SymbolState,
    cfg: Any,
    bench_ctx: BenchmarkContext,
    rps_slow: float | None,
) -> tuple[bool, bool]:
    momentum_ok = True
    ambush_ok = True
    if cfg.enable_rs_filter and bench_ctx.sorted_df is not None and not bench_ctx.sorted_df.empty:
        rs = calc_relative_strength(df_sorted, bench_ctx.sorted_df, cfg, bench_ctx.pct_by_date)
        if rs.rs_long is None or rs.rs_short is None:
            momentum_ok = False
            ambush_ok = False
        else:
            rs_long_min, rs_short_min = _effective_rs_thresholds(cfg, rs.bench_long_ret, rs.bench_short_ret)
            momentum_ok = rs.rs_long >= rs_long_min and rs.rs_short >= rs_short_min
            ambush_ok = rs.rs_long >= cfg.ambush_rs_long_min and rs.rs_short >= cfg.ambush_rs_short_min
    if not momentum_ok and _rs_structural_bypass_ok(df_sorted, state, rps_slow, cfg):
        momentum_ok = True
    return momentum_ok, ambush_ok


def _layer2_channels(
    df_sorted: pd.DataFrame,
    state: Layer2SymbolState,
    cfg: Any,
    bench_ctx: BenchmarkContext,
    rps_ctx: RpsContext,
    rps_state: Layer2RpsState,
    momentum_rs_ok: bool,
    ambush_rs_ok: bool,
    detect_sos: Callable[[pd.DataFrame, Any], float | None],
) -> dict[str, bool]:
    return {
        "momentum": _momentum_channel_ok(state, cfg, momentum_rs_ok, rps_state.momentum_ok),
        "ambush": ambush_channel_ok(
            cfg,
            close=state.close,
            last_close=state.last_close,
            last_ma_long=state.last_ma_long,
            rs_ok=ambush_rs_ok,
            rps_ok=rps_state.ambush_ok,
        ),
        "accum": accumulation_channel_ok(
            cfg,
            df_sorted=df_sorted,
            close=state.close,
            last_close=state.last_close,
            last_ma_short=state.last_ma_short,
            last_ma_long=state.last_ma_long,
        ),
        "dry_vol": dry_volume_channel_ok(cfg, df_sorted=df_sorted, close=state.close, last_close=state.last_close),
        "rs_div": rs_divergence_channel_ok(
            cfg, df_sorted=df_sorted, bench_sorted=bench_ctx.sorted_df, close=state.close, last_close=state.last_close
        ),
        "trend_cont": trend_continuation_channel_ok(
            cfg,
            df_sorted=df_sorted,
            close=state.close,
            bullish_alignment=state.bullish_alignment,
            rps_slow=rps_state.slow,
            active=rps_ctx.active,
        ),
        "breakout_accel": breakout_accel_channel_ok(
            cfg,
            df_sorted=df_sorted,
            close=state.close,
            last_close=state.last_close,
            last_ma_short=state.last_ma_short,
            bullish_alignment=state.bullish_alignment,
            rps_fast=rps_state.fast,
            active=rps_ctx.active,
        ),
        "sos": _sos_channel_ok(df_sorted, cfg, rps_ctx.active, rps_state.slow, detect_sos),
    }


def _momentum_channel_ok(state: Layer2SymbolState, cfg: Any, rs_ok: bool, rps_ok: bool) -> bool:
    bias_ok = True
    if pd.notna(state.last_ma_long) and float(state.last_ma_long) > 0 and pd.notna(state.last_close):
        bias_200 = (float(state.last_close) - float(state.last_ma_long)) / float(state.last_ma_long)
        bias_ok = bias_200 <= getattr(cfg, "momentum_bias_200_max", 0.25)
    return (state.bullish_alignment or state.holding_ma20) and rs_ok and rps_ok and bias_ok


def _sos_channel_ok(
    df_sorted: pd.DataFrame,
    cfg: Any,
    rps_active: bool,
    rps_slow: float | None,
    detect_sos: Callable[[pd.DataFrame, Any], float | None],
) -> bool:
    if not hasattr(cfg, "sos_vol_ratio"):
        return False
    sos_rps_ok = (not rps_active) or (rps_slow is not None and rps_slow >= float(cfg.sos_bypass_rps_slow_min))
    return sos_rps_ok and detect_sos(df_sorted, cfg) is not None


def _effective_rs_thresholds(
    cfg: Any, bench_long_ret: float | None, bench_short_ret: float | None
) -> tuple[float, float]:
    long_min = float(cfg.rs_min_long)
    short_min = float(cfg.rs_min_short)
    if not cfg.rs_dynamic_relax_enabled:
        return long_min, short_min
    long_hot = bench_long_ret is not None and bench_long_ret >= float(cfg.rs_bench_surge_long_pct)
    short_hot = bench_short_ret is not None and bench_short_ret >= float(cfg.rs_bench_surge_short_pct)
    if not (long_hot or short_hot):
        return long_min, short_min
    factor = max(0.0, min(float(cfg.rs_surge_relax_factor), 1.0))
    return long_min * factor, short_min * factor


def _rs_structural_bypass_ok(
    df_sorted: pd.DataFrame, state: Layer2SymbolState, rps_slow: float | None, cfg: Any
) -> bool:
    if not cfg.rs_structural_bypass_enabled or rps_slow is None:
        return False
    if rps_slow < float(cfg.rs_structural_bypass_rps_slow_min):
        return False
    if not (
        pd.notna(state.last_ma_short) and pd.notna(state.last_ma_long) and state.last_ma_short > state.last_ma_long > 0
    ):
        return False
    ret20 = close_return_pct(state.close, 20)
    if ret20 is None or ret20 < float(cfg.rs_structural_bypass_ret20_floor):
        return False
    bias50 = abs(state.last_close / float(state.last_ma_short) - 1.0)
    drawdown = _max_drawdown_pct(state.close, int(cfg.trend_cont_drawdown_window))
    return bias50 <= 0.12 and (drawdown is None or drawdown <= 18.0) and _rs_structural_volume_ok(df_sorted)


def _max_drawdown_pct(close: pd.Series, window: int) -> float | None:
    recent = pd.to_numeric(close, errors="coerce").dropna().tail(max(int(window), 2))
    if len(recent) < 2:
        return None
    drawdown = (recent - recent.cummax()) / recent.cummax() * 100.0
    return abs(float(drawdown.min()))


def _rs_structural_volume_ok(df_sorted: pd.DataFrame) -> bool:
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce").dropna()
    if len(vol) < 20:
        return True
    vol20 = float(vol.tail(20).mean())
    return vol20 <= 0 or float(vol.tail(5).mean()) / vol20 <= 1.8


def _benchmark_dropping(bench_sorted: pd.DataFrame, days: int, threshold: float) -> bool:
    if len(bench_sorted) < days:
        return False
    bench_cum = (bench_sorted.tail(days)["pct_chg"].dropna() / 100.0 + 1).prod() - 1
    return bool(bench_cum * 100 <= threshold)


def _rps_rows(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: Any,
    sort_frame: Callable[[pd.DataFrame], pd.DataFrame],
) -> list[tuple[str, float, float]]:
    rows: list[tuple[str, float, float]] = []
    for sym in symbols:
        df = df_map.get(sym)
        if df is None or df.empty:
            continue
        close = pd.to_numeric(sort_frame(df).get("close"), errors="coerce")
        ret_fast = close_return_pct(close, cfg.rps_window_fast)
        ret_slow = close_return_pct(close, cfg.rps_window_slow)
        if ret_fast is not None and ret_slow is not None:
            rows.append((sym, ret_fast, ret_slow))
    return rows


def _cum_return_pct_from_series(pct_series: pd.Series) -> float | None:
    pct = pd.to_numeric(pct_series, errors="coerce").dropna()
    return None if pct.empty else float(((pct / 100.0 + 1.0).prod() - 1.0) * 100.0)


def _rps_accel_bypass(cfg: Any, rps_fast: float, rps_slow: float, slope_value: float) -> bool:
    return (
        slope_value >= cfg.rps_slope_accel_bypass
        and rps_fast >= cfg.rps_accel_fast_min
        and rps_slow >= cfg.rps_accel_slow_min
    )


def _pre_ignition_volume_ok(df_sorted: pd.DataFrame, window: int, ratio_min: float) -> bool:
    if len(df_sorted) < 2 or "volume" not in df_sorted.columns:
        return False
    vol_tail = df_sorted["volume"].tail(window)
    if len(vol_tail) <= 1:
        return False
    prev_vol = float(df_sorted["volume"].iloc[-2])
    avg_vol = float(vol_tail.iloc[:-1].mean())
    return avg_vol > 0 and prev_vol / avg_vol >= ratio_min


def _low_position_ok(close: pd.Series, last_close: float, lookback: int, max_from_low: float) -> bool:
    period_low = float(close.tail(max(int(lookback), 2)).min())
    return period_low > 0 and float(last_close) <= period_low * (1.0 + max_from_low)


def _range_contract_ok(df_sorted: pd.DataFrame, window: int, max_pct: float) -> bool:
    zone = df_sorted.tail(max(int(window), 5))
    high = pd.to_numeric(zone.get("high"), errors="coerce")
    low = pd.to_numeric(zone.get("low"), errors="coerce")
    if high.dropna().empty or low.dropna().empty:
        return False
    high_max = float(high.max())
    low_min = float(low.min())
    return low_min > 0 and (high_max - low_min) / low_min * 100.0 <= max_pct


def _volume_dry_ok(df_sorted: pd.DataFrame, dry_window: int, ref_window: int, dry_ratio: float) -> bool:
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce")
    dry_w = max(int(dry_window), 2)
    ref_w = max(int(ref_window), dry_w + 1)
    recent_vol_mean = float(vol.tail(dry_w).mean()) if len(vol) >= dry_w else None
    ref_vol_mean = float(vol.tail(ref_w).iloc[:-dry_w].mean()) if len(vol) >= ref_w else None
    return (
        recent_vol_mean is not None
        and ref_vol_mean is not None
        and ref_vol_mean > 0
        and recent_vol_mean / ref_vol_mean < dry_ratio
    )


def _ma_gap_ok(last_ma_short: float, last_ma_long: float, gap_max: float) -> bool:
    if pd.isna(last_ma_short) or pd.isna(last_ma_long) or float(last_ma_long) <= 0:
        return False
    ma_gap_pct = (float(last_ma_short) - float(last_ma_long)) / float(last_ma_long) * 100.0
    return -gap_max * 100.0 <= ma_gap_pct <= gap_max * 100.0


def _rs_divergence_base_ok(cfg: Any, df_sorted: pd.DataFrame, bench_sorted: pd.DataFrame | None) -> bool:
    return (
        cfg.enable_rs_divergence_channel
        and bench_sorted is not None
        and not bench_sorted.empty
        and len(df_sorted) >= cfg.rs_div_bench_ref_window
    )


def _bench_made_lower_low(bench_close: pd.Series, cfg: Any) -> bool:
    recent = bench_close.tail(cfg.rs_div_bench_window)
    ref = bench_close.tail(cfg.rs_div_bench_ref_window).iloc[: -cfg.rs_div_bench_window]
    return not ref.dropna().empty and not recent.dropna().empty and float(recent.min()) < float(ref.min())


def _stock_higher_low_with_volume(df_sorted: pd.DataFrame, bench_sorted: pd.DataFrame, cfg: Any) -> bool:
    stock_low = pd.to_numeric(df_sorted.get("low"), errors="coerce")
    stock_recent = stock_low.tail(cfg.rs_div_stock_window)
    stock_ref = stock_low.tail(cfg.rs_div_bench_ref_window).iloc[: -cfg.rs_div_stock_window]
    if stock_ref.dropna().empty or stock_recent.dropna().empty:
        return False
    if float(stock_recent.min()) < float(stock_ref.min()):
        return False
    return _rs_divergence_volume_ok(df_sorted, bench_sorted, cfg)


def _rs_divergence_volume_ok(df_sorted: pd.DataFrame, bench_sorted: pd.DataFrame, cfg: Any) -> bool:
    bench_vol = pd.to_numeric(bench_sorted.get("volume"), errors="coerce")
    stock_vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce")
    if bench_vol.empty or stock_vol.empty:
        return True
    bench_recent_vol = bench_vol.tail(cfg.rs_div_bench_window).mean()
    bench_ref_vol = bench_vol.tail(cfg.rs_div_bench_ref_window).iloc[: -cfg.rs_div_bench_window].mean()
    stock_recent_vol = stock_vol.tail(cfg.rs_div_stock_window).mean()
    stock_ref_vol = stock_vol.tail(cfg.rs_div_bench_ref_window).iloc[: -cfg.rs_div_stock_window].mean()
    if bench_ref_vol <= 0 or stock_ref_vol <= 0:
        return True
    return bench_recent_vol > bench_ref_vol * 1.2 and stock_recent_vol < stock_ref_vol * 0.8


def _breakout_volume_ok(df_sorted: pd.DataFrame, cfg: Any) -> bool:
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce")
    ret_window = cfg.breakout_accel_ret_window
    ref_window = cfg.breakout_accel_vol_ref_window
    if len(vol) < ref_window:
        return False
    recent_vol = float(vol.tail(ret_window).mean())
    ref_vol = float(vol.tail(ref_window).iloc[:-ret_window].mean())
    return ref_vol > 0 and recent_vol / ref_vol >= cfg.breakout_accel_vol_ratio


def _trend_drawdown_ok(close: pd.Series, window: int, max_drawdown_pct: float) -> bool:
    recent_close = close.tail(max(int(window), 10))
    if len(recent_close) < 10:
        return False
    drawdown = (recent_close - recent_close.cummax()) / recent_close.cummax() * 100.0
    return abs(float(drawdown.min())) < max_drawdown_pct


def _trend_volume_ok(df_sorted: pd.DataFrame, min_ratio: float) -> bool:
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce").dropna()
    if len(vol) < 20:
        return True
    vol20 = float(vol.tail(20).mean())
    return vol20 <= 0 or float(vol.tail(5).mean()) / vol20 >= min_ratio


def _diagnose_momentum(cfg: Any, rps_slow: float | None, momentum_rs_ok: bool) -> tuple[float, list[str]]:
    if not getattr(cfg, "enable_momentum_channel", True):
        return 999.0, ["通道未启用"]
    gaps = []
    fail = []
    if not momentum_rs_ok:
        gaps.append(0.5)
        fail.append("RS强度未确认: 当前未通过")
    thresh = cfg.rps_slow_min
    val = rps_slow or 0.0
    if val < thresh:
        gaps.append((thresh - val) / thresh)
        fail.append(f"RPS(slow)不足: 当前 {val:.1f}, 阈值 {thresh:.1f}, 差距 {thresh - val:.1f}")
    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _diagnose_ambush(
    cfg: Any,
    last_ma_long: float | None,
    last_close: float | None,
    close_series: pd.Series,
    ambush_rs_ok: bool,
    rps_slow: float | None,
) -> tuple[float, list[str]]:
    if not getattr(cfg, "enable_ambush_channel", True):
        return 999.0, ["通道未启用"]
    if pd.isna(last_ma_long) or float(last_ma_long) <= 0 or pd.isna(last_close):
        return 1.0, ["均线或价格数据缺失"]

    gaps = []
    fail = []
    bias_200 = (float(last_close) - float(last_ma_long)) / float(last_ma_long)
    ret20 = close_return_pct(close_series, 20)
    bias_ok = abs(bias_200) <= cfg.ambush_bias_200_abs_max
    ret20_ok = ret20 is not None and ret20 <= cfg.ambush_ret20_max

    if not bias_ok:
        thresh = cfg.ambush_bias_200_abs_max
        val = abs(bias_200)
        gaps.append((val - thresh) / thresh)
        fail.append(
            f"偏离MA200超标: 当前 {bias_200 * 100:+.1f}%, 阈值 ±{thresh * 100:.1f}%, 差距 {(val - thresh) * 100:.1f}%"
        )
    if not ret20_ok:
        thresh = cfg.ambush_ret20_max
        val = ret20 or 0.0
        gaps.append((val - thresh) / thresh if thresh > 0 else 0.5)
        fail.append(f"20日涨幅超标: 当前 {val:+.1f}%, 阈值 {thresh:.1f}%, 差距 {val - thresh:.1f}%")
    if not ambush_rs_ok:
        gaps.append(0.5)
        fail.append("RS强度未确认: 当前未通过")
    thresh_rps = cfg.ambush_rps_slow_min
    val_rps = rps_slow or 0.0
    if val_rps < thresh_rps:
        gaps.append((thresh_rps - val_rps) / thresh_rps)
        fail.append(f"RPS(slow)不足: 当前 {val_rps:.1f}, 阈值 {thresh_rps:.1f}, 差距 {thresh_rps - val_rps:.1f}")

    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _accum_low_gap(cfg: Any, last_close: float | None, close_series: pd.Series) -> tuple[float | None, str | None]:
    lookback = int(cfg.accum_lookback_days)
    max_from_low = float(cfg.accum_price_from_low_max)
    period_low = float(close_series.tail(max(lookback, 2)).min())
    if period_low > 0:
        price_from_low = float(last_close) / period_low - 1.0
        if price_from_low > max_from_low:
            gap = (price_from_low - max_from_low) / max_from_low
            return gap, (
                f"偏离低位过高: 当前 {price_from_low * 100:.1f}%, "
                f"阈值 {max_from_low * 100:.1f}%, 差距 {(price_from_low - max_from_low) * 100:.1f}%"
            )
    else:
        return 0.5, "低位价格无效"
    return None, None


def _accum_range_gap(cfg: Any, df_sorted: pd.DataFrame) -> tuple[float | None, str | None]:
    window = int(cfg.accum_range_window)
    max_pct = float(cfg.accum_range_max_pct)
    zone = df_sorted.tail(max(window, 5))
    high = pd.to_numeric(zone.get("high"), errors="coerce")
    low = pd.to_numeric(zone.get("low"), errors="coerce")
    if not high.dropna().empty and not low.dropna().empty:
        high_max = float(high.max())
        low_min = float(low.min())
        range_pct = (high_max - low_min) / low_min * 100.0 if low_min > 0 else 999.0
        if range_pct > max_pct:
            gap = (range_pct - max_pct) / max_pct
            return gap, f"振幅未收敛: 当前 {range_pct:.1f}%, 阈值 {max_pct:.1f}%, 差距 {range_pct - max_pct:.1f}%"
    else:
        return 0.5, "振幅高低点无效"
    return None, None


def _accum_vol_gap(cfg: Any, df_sorted: pd.DataFrame) -> tuple[float | None, str | None]:
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce")
    dry_w = max(int(cfg.accum_vol_dry_window), 2)
    ref_w = max(int(cfg.accum_vol_dry_ref_window), dry_w + 1)
    recent_vol_mean = float(vol.tail(dry_w).mean()) if len(vol) >= dry_w else None
    ref_vol_mean = float(vol.tail(ref_w).iloc[:-dry_w].mean()) if len(vol) >= ref_w else None
    dry_ratio = float(cfg.accum_vol_dry_ratio)
    if recent_vol_mean is not None and ref_vol_mean is not None and ref_vol_mean > 0:
        vol_ratio = recent_vol_mean / ref_vol_mean
        if vol_ratio >= dry_ratio:
            gap = (vol_ratio - dry_ratio) / dry_ratio
            return gap, f"成交量未萎缩: 当前 {vol_ratio:.2f}x, 阈值 {dry_ratio:.2f}x, 差距 {vol_ratio - dry_ratio:.2f}x"
    else:
        return 0.5, "成交量数据不足"
    return None, None


def _accum_ma_gap(cfg: Any, last_ma_short: float | None, last_ma_long: float | None) -> tuple[float | None, str | None]:
    gap_max = float(cfg.accum_ma_gap_max)
    if pd.notna(last_ma_short) and pd.notna(last_ma_long) and float(last_ma_long) > 0:
        ma_gap_pct = (float(last_ma_short) - float(last_ma_long)) / float(last_ma_long) * 100.0
        if abs(ma_gap_pct) > gap_max * 100.0:
            gap = (abs(ma_gap_pct) / 100.0 - gap_max) / gap_max
            return (
                gap,
                f"均线间距过大: 当前 {ma_gap_pct:+.1f}%, 阈值 ±{gap_max * 100:.1f}%, 差距 {abs(ma_gap_pct) - gap_max * 100:.1f}%",
            )
    else:
        return 0.5, "均线数据无效"
    return None, None


def _diagnose_accum(
    cfg: Any,
    df_sorted: pd.DataFrame,
    close_series: pd.Series,
    last_close: float | None,
    last_ma_short: float | None,
    last_ma_long: float | None,
) -> tuple[float, list[str]]:
    if not getattr(cfg, "enable_accumulation_channel", True):
        return 999.0, ["通道未启用"]
    if len(df_sorted) < max(cfg.accum_lookback_days, cfg.accum_vol_dry_ref_window):
        return 1.0, ["历史长度不足"]

    gaps = []
    fail = []

    for gap, desc in [
        _accum_low_gap(cfg, last_close, close_series),
        _accum_range_gap(cfg, df_sorted),
        _accum_vol_gap(cfg, df_sorted),
        _accum_ma_gap(cfg, last_ma_short, last_ma_long),
    ]:
        if gap is not None and desc is not None:
            gaps.append(gap)
            fail.append(desc)

    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _diagnose_dry_vol(
    cfg: Any, df_sorted: pd.DataFrame, close_series: pd.Series, last_close: float | None
) -> tuple[float, list[str]]:
    if not getattr(cfg, "enable_dry_vol_channel", True):
        return 999.0, ["通道未启用"]
    if len(df_sorted) < cfg.dry_vol_ref_window:
        return 1.0, ["历史长度不足"]

    gaps = []
    fail = []

    # 1. Low position
    lookback = int(cfg.dry_vol_ref_window)
    max_from_low = float(cfg.dry_vol_price_from_low_max)
    period_low = float(close_series.tail(max(lookback, 2)).min())
    if period_low > 0:
        price_from_low = float(last_close) / period_low - 1.0
        if price_from_low > max_from_low:
            gaps.append((price_from_low - max_from_low) / max_from_low)
            fail.append(
                f"偏离低位过高: 当前 {price_from_low * 100:.1f}%, 阈值 {max_from_low * 100:.1f}%, 差距 {(price_from_low - max_from_low) * 100:.1f}%"
            )

    # 2. Dry vol quantile
    vol = pd.to_numeric(df_sorted.get("volume"), errors="coerce")
    ref_vol = vol.tail(max(int(cfg.dry_vol_ref_window), 2))
    if len(ref_vol.dropna()) < 50:
        gaps.append(0.5)
        fail.append("有效量数据不足")
    else:
        vol_threshold = float(np.quantile(ref_vol.dropna().values, cfg.dry_vol_quantile))
        min_vol = float(vol.tail(cfg.dry_vol_lookback).min())
        if min_vol > vol_threshold:
            gaps.append((min_vol - vol_threshold) / vol_threshold)
            fail.append(
                f"未见地量: 当前最小量 {min_vol:.0f}, 阈值量 {vol_threshold:.0f}, 差距 {min_vol - vol_threshold:.0f}"
            )

    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _diagnose_rs_div(
    cfg: Any, df_sorted: pd.DataFrame, close_series: pd.Series, last_close: float | None, bench_ctx: Any
) -> tuple[float, list[str]]:
    if not _rs_divergence_base_ok(cfg, df_sorted, bench_ctx.sorted_df):
        return 999.0, ["不满足底背离基础条件"]

    gaps = []
    fail = []

    # 1. Low position
    lookback = max(int(cfg.dry_vol_ref_window), 250)
    max_from_low = float(cfg.rs_div_price_from_low_max)
    period_low = float(close_series.tail(max(lookback, 2)).min())
    if period_low > 0:
        price_from_low = float(last_close) / period_low - 1.0
        if price_from_low > max_from_low:
            gaps.append((price_from_low - max_from_low) / max_from_low)
            fail.append(
                f"偏离低位过高: 当前 {price_from_low * 100:.1f}%, 阈值 {max_from_low * 100:.1f}%, 差距 {(price_from_low - max_from_low) * 100:.1f}%"
            )

    # 2. Bench new low
    bench_close = pd.to_numeric(bench_ctx.sorted_df.get("close"), errors="coerce")
    if len(bench_close.dropna()) < cfg.rs_div_bench_ref_window:
        gaps.append(0.5)
        fail.append("大盘数据不足")
    else:
        if not _bench_made_lower_low(bench_close, cfg):
            gaps.append(0.5)
            fail.append("大盘未创收盘新低: 未通过")
        if not _stock_higher_low_with_volume(df_sorted, bench_ctx.sorted_df, cfg):
            gaps.append(0.5)
            fail.append("个股未创高低点或未放量: 未通过")

    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _diagnose_trend_cont(
    cfg: Any, df_sorted: pd.DataFrame, close_series: pd.Series, rps_slow: float | None, state: Any
) -> tuple[float, list[str]]:
    if not getattr(cfg, "enable_trend_cont_channel", True):
        return 999.0, ["通道未启用"]

    gaps = []
    fail = []
    if not state.bullish_alignment:
        gaps.append(0.5)
        fail.append("均线未多头排列: 未通过")
    thresh_rps = cfg.trend_cont_rps_slow_min
    val_rps = rps_slow or 0.0
    if val_rps < thresh_rps:
        gaps.append((thresh_rps - val_rps) / thresh_rps)
        fail.append(f"RPS(slow)不足: 当前 {val_rps:.1f}, 阈值 {thresh_rps:.1f}, 差距 {thresh_rps - val_rps:.1f}")

    drawdown_ok = _trend_drawdown_ok(
        close_series, int(cfg.trend_cont_drawdown_window), float(cfg.trend_cont_max_drawdown_pct)
    )
    if not drawdown_ok:
        gaps.append(0.4)
        fail.append("趋势回撤超标: 未通过")
    if not _trend_volume_ok(df_sorted, float(cfg.trend_cont_vol_ratio_min)):
        gaps.append(0.4)
        fail.append("成交量不合规: 未通过")

    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _diagnose_breakout_accel(
    cfg: Any,
    df_sorted: pd.DataFrame,
    close_series: pd.Series,
    last_close: float | None,
    last_ma_short: float | None,
    rps_fast: float | None,
    state: Any,
) -> tuple[float, list[str]]:
    if not getattr(cfg, "enable_breakout_accel_channel", True):
        return 999.0, ["通道未启用"]

    gaps = []
    fail = []
    if pd.notna(last_ma_short) and pd.notna(last_close):
        if float(last_close) <= float(last_ma_short):
            gaps.append((float(last_ma_short) - float(last_close)) / float(last_ma_short))
            fail.append(f"未在MA50上方: 当前 {last_close:.2f}, 阈值MA50 {last_ma_short:.2f}")
    else:
        gaps.append(0.5)
        fail.append("均线或收盘价数据缺失")

    if state.bullish_alignment:
        gaps.append(0.5)
        fail.append("已均线多头排列(点火通道仅限突破前): 未通过")

    thresh_rps = cfg.breakout_accel_rps_fast_min
    val_rps = rps_fast or 0.0
    if val_rps < thresh_rps:
        gaps.append((thresh_rps - val_rps) / thresh_rps)
        fail.append(f"RPS(fast)不足: 当前 {val_rps:.1f}, 阈值 {thresh_rps:.1f}, 差距 {thresh_rps - val_rps:.1f}")

    ret = close_return_pct(close_series, cfg.breakout_accel_ret_window)
    thresh_ret = cfg.breakout_accel_ret_min
    if ret is None or ret < thresh_ret:
        val_ret = ret or 0.0
        gaps.append((thresh_ret - val_ret) / thresh_ret if thresh_ret > 0 else 0.5)
        fail.append(f"涨幅不足: 当前 {val_ret:+.1f}%, 阈值 {thresh_ret:.1f}%, 差距 {thresh_ret - val_ret:.1f}%")

    if not _breakout_volume_ok(df_sorted, cfg):
        gaps.append(0.4)
        fail.append("放量不足: 未通过")

    if not gaps:
        return 0.0, []
    return max(gaps), fail


def _diagnose_sos(
    cfg: Any,
    df_sorted: pd.DataFrame,
    rps_ctx: RpsContext,
    rps_slow: float | None,
    detect_sos: Callable[[pd.DataFrame, Any], float | None],
) -> tuple[float, list[str]]:
    if not hasattr(cfg, "sos_vol_ratio"):
        return 999.0, ["通道未启用"]
    gaps = []
    reasons = []
    threshold = float(cfg.sos_bypass_rps_slow_min)
    if rps_ctx.active and (rps_slow is None or rps_slow < threshold):
        value = float(rps_slow or 0.0)
        gaps.append((threshold - value) / max(threshold, 1.0))
        reasons.append(f"RPS(slow)不足: 当前 {value:.1f}, 阈值 {threshold:.1f}, 差距 {threshold - value:.1f}")
    if detect_sos(df_sorted, cfg) is None:
        gaps.append(0.5)
        reasons.append("未形成放量突破阻力的SOS结构")
    return (max(gaps), reasons) if gaps else (0.0, [])


def diagnose_layer2_symbol_failure(
    sym: str,
    df_sorted: pd.DataFrame,
    cfg: Any,
    *,
    bench_ctx: BenchmarkContext,
    rps_ctx: RpsContext,
    rps_state: Layer2RpsState,
    momentum_rs_ok: bool,
    ambush_rs_ok: bool,
    detect_sos: Callable[[pd.DataFrame, Any], float | None],
) -> str:
    state = _symbol_state(df_sorted, cfg, bench_ctx)
    close_series = state.close
    last_close = state.last_close
    last_ma_short = state.last_ma_short
    last_ma_long = state.last_ma_long
    rps_slow = rps_state.slow
    rps_fast = rps_state.fast

    channel_failures = {
        "主升": _diagnose_momentum(cfg, rps_slow, momentum_rs_ok),
        "潜伏": _diagnose_ambush(cfg, last_ma_long, last_close, close_series, ambush_rs_ok, rps_slow),
        "吸筹": _diagnose_accum(cfg, df_sorted, close_series, last_close, last_ma_short, last_ma_long),
        "地量蓄势": _diagnose_dry_vol(cfg, df_sorted, close_series, last_close),
        "暗中护盘": _diagnose_rs_div(cfg, df_sorted, close_series, last_close, bench_ctx),
        "趋势延续": _diagnose_trend_cont(cfg, df_sorted, close_series, rps_slow, state),
        "加速突破": _diagnose_breakout_accel(cfg, df_sorted, close_series, last_close, last_ma_short, rps_fast, state),
        "点火破局": _diagnose_sos(cfg, df_sorted, rps_ctx, rps_slow, detect_sos),
    }

    valid_channels = {k: v for k, v in channel_failures.items() if "通道未启用" not in v[1]}
    if not valid_channels:
        valid_channels = channel_failures

    # Sort by gap float value (v[0])
    sorted_fails = sorted(valid_channels.items(), key=lambda item: item[1][0])
    closest_name, (closest_gap, closest_reasons) = sorted_fails[0]

    return f"最接近通道[{closest_name}](缺口{closest_gap * 100:.1f}%): {', '.join(closest_reasons)}"
