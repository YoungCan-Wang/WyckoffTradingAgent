"""Layer 2 strength calculation helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.layer2_strength import (
    Layer2SymbolState,
    _diagnose_momentum,
    build_benchmark_context,
    build_rps_context,
    calc_relative_strength,
    channel_labels,
    close_return_pct,
    rps_filter_flags,
    trend_continuation_channel_ok,
)
from core.trend_drawdown_risk import (
    annotate_trend_drawdown_risk,
    classify_trend_drawdown,
    classify_trend_drawdown_pct,
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


def _volatile_trend_frame() -> pd.DataFrame:
    close = [60.0 + i * 0.20 for i in range(140)] + [100.0, 68.0] + [82.0 + i * 0.45 for i in range(58)]
    return pd.DataFrame({"close": close, "volume": [1_000_000.0] * len(close)})


def test_trend_continuation_no_longer_hard_blocks_large_historical_drawdown() -> None:
    frame = _volatile_trend_frame()
    cfg = SimpleNamespace(
        enable_trend_cont_channel=True,
        trend_cont_rps_slow_min=75.0,
        trend_cont_vol_ratio_min=0.70,
    )

    assert trend_continuation_channel_ok(
        cfg,
        df_sorted=frame,
        close=frame["close"],
        bullish_alignment=True,
        rps_slow=90.0,
        active=True,
    )


def test_trend_drawdown_becomes_candidate_risk_metadata() -> None:
    frame = _volatile_trend_frame()
    risk = classify_trend_drawdown(frame["close"])
    entries = [{"code": "000001", "risk": "仍需 confirmed 确认"}]

    annotate_trend_drawdown_risk(entries, {"000001": frame}, {"000001": "趋势延续"})

    assert risk is not None and risk.drawdown_pct >= 30.0
    assert "60日极高波动" in entries[0]["risk"]
    assert entries[0]["metrics"]["trend_drawdown60_pct"] >= 30.0


def test_trend_drawdown_penalty_starts_at_high_risk_boundary() -> None:
    assert classify_trend_drawdown_pct(19.99).rank_penalty == 0.0
    assert classify_trend_drawdown_pct(20.0).rank_penalty == 0.02
    assert classify_trend_drawdown_pct(30.0).rank_penalty == 0.04


def test_diagnose_layer2_symbol_failure(monkeypatch) -> None:
    from core.layer2_strength import Layer2RpsState, diagnose_layer2_symbol_failure
    from core.wyckoff_engine import (
        FunnelConfig,
        build_benchmark_context,
        build_layer2_evaluation_context,
        build_rps_context,
        layer2_strength_detailed,
    )

    cfg = FunnelConfig()
    row_count = 320
    dates = pd.date_range("2024-01-01", periods=row_count)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * row_count,
            "high": [10.2] * row_count,
            "low": [9.8] * row_count,
            "close": [10.0] * row_count,
            "volume": [1000] * row_count,
            "pct_chg": [0.0] * row_count,
        }
    )

    bench_df = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * row_count,
            "high": [10.2] * row_count,
            "low": [9.8] * row_count,
            "close": [10.0] * row_count,
            "volume": [1000] * row_count,
            "pct_chg": [0.0] * row_count,
        }
    )

    bench_ctx = build_benchmark_context(
        bench_df, cfg, sort_frame=lambda x: x, latest_trade_date=lambda x: x["date"].iloc[-1]
    )
    rps_ctx = build_rps_context(["000001"], {"000001": df}, cfg, rps_universe=["000001"], sort_frame=lambda x: x)
    rps_state = Layer2RpsState(slow=50.0, fast=50.0, momentum_ok=False, ambush_ok=False)

    res = diagnose_layer2_symbol_failure(
        "000001",
        df,
        cfg,
        bench_ctx=bench_ctx,
        rps_ctx=rps_ctx,
        rps_state=rps_state,
        momentum_rs_ok=False,
        ambush_rs_ok=False,
        detect_sos=lambda _df, _cfg: None,
    )

    assert "最接近通道" in res
    assert "缺口" in res

    cfg.enable_dry_vol_channel = False
    evaluation_context = build_layer2_evaluation_context(
        ["000001"],
        {"000001": df},
        bench_df,
        cfg,
        rps_universe=["000001"],
    )

    def fail_rebuild(*_args, **_kwargs):
        raise AssertionError("Layer2 context should be reused")

    monkeypatch.setattr("core.wyckoff_engine.build_benchmark_context", fail_rebuild)
    monkeypatch.setattr("core.wyckoff_engine.build_rps_context", fail_rebuild)
    rejections = {}
    layer2_strength_detailed(
        ["000001"],
        {"000001": df},
        bench_df,
        cfg,
        rejections=rejections,
        evaluation_context=evaluation_context,
    )

    assert "000001" in rejections
    assert "诊断失败" not in rejections["000001"]


def _momentum_state(*, last_close: float, last_ma_long: float, alignment: bool, holding: bool):
    return Layer2SymbolState(
        close=pd.Series([last_close]),
        last_close=last_close,
        last_ma_short=last_close,
        last_ma_long=last_ma_long,
        bullish_alignment=alignment,
        holding_ma20=holding,
    )


def test_momentum_diagnosis_reports_ma200_overextension() -> None:
    """回归：被 MA200 乖离上限拦下的票此前显示缺口 0.0% 且原因为空。"""
    cfg = SimpleNamespace(rps_slow_min=75.0, momentum_bias_200_max=0.25)
    state = _momentum_state(last_close=125.87, last_ma_long=92.01, alignment=False, holding=True)

    gap, reasons = _diagnose_momentum(cfg, 90.0, True, state)

    assert gap > 0
    assert any("偏离MA200过高" in reason for reason in reasons)


def test_momentum_diagnosis_reports_broken_ma_structure() -> None:
    cfg = SimpleNamespace(rps_slow_min=75.0, momentum_bias_200_max=0.25)
    state = _momentum_state(last_close=90.0, last_ma_long=100.0, alignment=False, holding=False)

    gap, reasons = _diagnose_momentum(cfg, 90.0, True, state)

    assert gap > 0
    assert any("均线结构未确认" in reason for reason in reasons)


def test_momentum_diagnosis_stays_clean_when_structure_passes() -> None:
    cfg = SimpleNamespace(rps_slow_min=75.0, momentum_bias_200_max=0.25)
    state = _momentum_state(last_close=110.0, last_ma_long=100.0, alignment=True, holding=True)

    assert _diagnose_momentum(cfg, 90.0, True, state) == (0.0, [])


def test_momentum_diagnosis_reports_fast_rps_and_slope() -> None:
    cfg = SimpleNamespace(
        rps_slow_min=75.0,
        rps_fast_min=80.0,
        rps_slope_min=0.5,
        momentum_bias_200_max=0.25,
    )
    state = _momentum_state(last_close=110.0, last_ma_long=100.0, alignment=True, holding=True)

    gap, reasons = _diagnose_momentum(
        cfg,
        80.0,
        True,
        state,
        rps_fast=70.0,
        slope_ok=False,
        slope_value=-0.2,
        momentum_rps_ok=False,
    )

    assert gap > 0
    assert any("RPS(fast)不足" in reason for reason in reasons)
    assert any("RPS斜率不足" in reason for reason in reasons)
