"""Layer 2 strength calculation helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.layer2_strength import (
    build_benchmark_context,
    build_rps_context,
    calc_relative_strength,
    channel_labels,
    close_return_pct,
    rps_filter_flags,
)


def test_close_return_pct_uses_lookback_start() -> None:
    close = pd.Series([10.0, 11.0, 12.0])

    assert close_return_pct(close, 2) == 20.0


def test_benchmark_context_detects_drop() -> None:
    cfg = SimpleNamespace(bench_drop_days=3, bench_drop_threshold=-2.0)
    bench = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3), "pct_chg": [-1.0, -1.0, -1.0]})

    ctx = build_benchmark_context(
        bench, cfg, sort_frame=lambda df: df, latest_trade_date=lambda df: df["date"].iloc[-1]
    )

    assert ctx.dropping is True
    assert ctx.latest_date == bench["date"].iloc[-1]


def test_relative_strength_returns_stock_minus_benchmark() -> None:
    cfg = SimpleNamespace(rs_window_long=2, rs_window_short=1)
    dates = pd.date_range("2024-01-01", periods=2)
    stock = pd.DataFrame({"date": dates, "pct_chg": [10.0, 0.0]})
    bench = pd.DataFrame({"date": dates, "pct_chg": [0.0, 0.0]})

    rs = calc_relative_strength(stock, bench, cfg)

    assert round(rs.rs_long, 6) == 10.0
    assert rs.rs_short == 0.0


def test_rps_context_ranks_full_universe() -> None:
    cfg = SimpleNamespace(enable_rps_filter=True, rps_window_fast=2, rps_window_slow=2)
    dates = pd.date_range("2024-01-01", periods=3)
    df_map = {
        "A": pd.DataFrame({"date": dates, "close": [10.0, 10.0, 11.0]}),
        "B": pd.DataFrame({"date": dates, "close": [10.0, 10.0, 12.0]}),
    }

    ctx = build_rps_context(["A"], df_map, cfg, rps_universe=["A", "B"], sort_frame=lambda df: df)

    assert ctx.active is True
    assert ctx.slow["B"] > ctx.slow["A"]


def test_rps_filter_flags_allow_accel_bypass() -> None:
    cfg = SimpleNamespace(
        enable_rps_filter=True,
        rps_fast_min=65.0,
        rps_slow_min=70.0,
        rps_slow_strong_bypass=80.0,
        rps_fast_bypass_min=50.0,
        rps_slope_accel_bypass=1.5,
        rps_accel_fast_min=50.0,
        rps_accel_slow_min=55.0,
        ambush_rps_fast_max=45.0,
        ambush_rps_slow_min=70.0,
    )

    momentum_ok, ambush_ok = rps_filter_flags(
        cfg,
        active=True,
        rps_fast=55.0,
        rps_slow=60.0,
        slope_ok=False,
        slope_value=2.0,
    )

    assert momentum_ok is True
    assert ambush_ok is False


def test_channel_labels_preserve_order_and_return_empty_without_hits() -> None:
    assert channel_labels({"ambush": True, "sos": True}) == ["潜伏通道", "点火破局"]
    assert channel_labels({}) == []
