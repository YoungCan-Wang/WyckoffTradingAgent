"""Layer 2 strength calculation helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.layer2_strength import (
    BenchmarkContext,
    Layer2RpsState,
    Layer2SymbolState,
    RpsContext,
    _benchmark_regime_gate_passed,
    _diagnose_ambush,
    _diagnose_momentum,
    _diagnose_sos,
    _sos_channel_ok,
    ambush_channel_ok,
    build_benchmark_context,
    build_rps_context,
    calc_relative_strength,
    channel_labels,
    close_return_pct,
    diagnose_layer2_symbol_failure,
    evaluate_layer2_symbol,
    rps_filter_flags,
    trend_continuation_channel_ok,
)
from core.trend_drawdown_risk import (
    annotate_trend_drawdown_risk,
    classify_trend_drawdown,
    classify_trend_drawdown_pct,
)
from core.wyckoff_engine import FunnelConfig


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
    assert "60日深回撤" in entries[0]["risk"]
    assert entries[0]["metrics"]["trend_drawdown60_pct"] >= 30.0


def test_trend_drawdown_penalty_starts_at_high_risk_boundary() -> None:
    assert classify_trend_drawdown_pct(19.99).rank_penalty == 0.0
    assert classify_trend_drawdown_pct(20.0).rank_penalty == 0.02
    assert classify_trend_drawdown_pct(30.0).rank_penalty == 0.04


def test_trend_drawdown_labels_match_measured_semantics() -> None:
    """标签必须只声称数据支持的那件事。

    实测（scripts/ablate_trend_drawdown_gate.py）：20% 处分离的是波动（+2.22pct，随机
    带宽 [−0.43,+0.22]）；20–30% 与 >=30% 的波动差落在随机带宽内不可分，可分的是下行
    （MAE 差 −0.996pct）。故第二档是「深回撤」而非「极高波动」。
    """
    assert classify_trend_drawdown_pct(25.0).label.startswith("60日高波动")
    assert classify_trend_drawdown_pct(35.0).label.startswith("60日深回撤")
    assert "极高波动" not in classify_trend_drawdown_pct(35.0).label
    assert classify_trend_drawdown_pct(15.0).label == ""


def test_trend_drawdown_window_shared_with_funnel_config() -> None:
    """风险标签与 RS 结构旁路必须共用同一个「60 日」，否则改一处不同步。"""
    from core.trend_drawdown_risk import TREND_DRAWDOWN_WINDOW
    from core.wyckoff_engine import FunnelConfig

    assert FunnelConfig().trend_cont_drawdown_window == TREND_DRAWDOWN_WINDOW


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


def test_benchmark_regime_gate_passed_evaluates_ma50() -> None:
    cfg_off = SimpleNamespace(enable_market_regime_gate=False)
    bench_down = pd.DataFrame({"close": [100.0] * 50 + [80.0]})
    assert _benchmark_regime_gate_passed(bench_down, cfg_off) is True

    cfg_on = SimpleNamespace(enable_market_regime_gate=True, market_regime_gate_ma=50)
    bench_up = pd.DataFrame({"close": [100.0] * 50 + [105.0]})
    assert _benchmark_regime_gate_passed(bench_up, cfg_on) is True

    bench_below = pd.DataFrame({"close": [100.0] * 50 + [90.0]})
    assert _benchmark_regime_gate_passed(bench_below, cfg_on) is False

    # Test buffer_pct (1% buffer)
    cfg_band = SimpleNamespace(
        enable_market_regime_gate=True, market_regime_gate_ma=20, market_regime_gate_buffer_pct=0.01
    )
    bench_barely_above = pd.DataFrame({"close": [100.0] * 20 + [100.5]})
    assert _benchmark_regime_gate_passed(bench_barely_above, cfg_band) is False  # 100.5 < 101.0
    bench_well_above = pd.DataFrame({"close": [100.0] * 20 + [101.5]})
    assert _benchmark_regime_gate_passed(bench_well_above, cfg_band) is True  # 101.5 >= 101.0


def test_evaluate_layer2_symbol_blocks_when_regime_gate_fails() -> None:
    cfg = FunnelConfig(
        enable_market_regime_gate=True,
        market_regime_gate_ma=50,
    )
    df = pd.DataFrame({"close": [100.0] * 30, "volume": [1000.0] * 30})
    bench_ctx = BenchmarkContext(sorted_df=None, latest_date=None, dropping=False, regime_gate_passed=False)
    rps_ctx = RpsContext(fast={}, slow={}, active=False)

    res = evaluate_layer2_symbol(
        "600000",
        df,
        cfg,
        bench_ctx=bench_ctx,
        rps_ctx=rps_ctx,
        detect_sos=lambda d, c: None,
    )
    assert res.passed is False
    assert res.channel == ""

    rps_state = Layer2RpsState(None, None, True, True, True, 0.0)
    diag = diagnose_layer2_symbol_failure(
        "600000",
        df,
        cfg,
        bench_ctx=bench_ctx,
        rps_ctx=rps_ctx,
        rps_state=rps_state,
        momentum_rs_ok=True,
        ambush_rs_ok=True,
        detect_sos=lambda d, c: None,
    )
    assert "大盘处于MA50空头生命线下方(市场门控拦截)" in diag


def test_diagnose_layer2_symbol_failure_ignores_disabled_channels() -> None:
    cfg = FunnelConfig(
        enable_rs_divergence_channel=False,
        enable_breakout_accel_channel=False,
    )
    closes = [10.0] * 100
    df = pd.DataFrame({"close": closes, "volume": [1000.0] * 100})
    bench_ctx = BenchmarkContext(sorted_df=None, latest_date=None, dropping=False, regime_gate_passed=True)
    rps_ctx = RpsContext(fast={}, slow={}, active=False)
    rps_state = Layer2RpsState(None, None, False, False, True, 0.0)

    diag = diagnose_layer2_symbol_failure(
        "600000",
        df,
        cfg,
        bench_ctx=bench_ctx,
        rps_ctx=rps_ctx,
        rps_state=rps_state,
        momentum_rs_ok=False,
        ambush_rs_ok=False,
        detect_sos=lambda d, c: None,
    )
    assert "暗中护盘" not in diag
    assert "加速突破" not in diag


def test_diagnose_sos_reports_disabled_when_attribute_missing() -> None:
    cfg_no_sos = SimpleNamespace()
    df = pd.DataFrame({"close": [10.0] * 10, "volume": [1000.0] * 10})
    rps_ctx = RpsContext(fast={}, slow={}, active=False)
    gap, reasons = _diagnose_sos(cfg_no_sos, df, rps_ctx, rps_slow=None, detect_sos=lambda d, c: None)
    assert gap == 999.0
    assert "通道未启用" in reasons


def test_sos_channel_and_diagnose_consistency() -> None:
    """验证生产通道判定与诊断函数严格一致：生产判定 True ⇔ 诊断缺口 0.0。"""
    cfg = FunnelConfig()
    df = pd.DataFrame({"close": [10.0] * 30, "volume": [1000.0] * 30})
    rps_ctx = RpsContext(fast={}, slow={}, active=True)
    sos_passed = _sos_channel_ok(df, cfg, rps_active=True, rps_slow=80.0, detect_sos=lambda d, c: 10.0)
    assert sos_passed is True
    gap, reasons = _diagnose_sos(cfg, df, rps_ctx, rps_slow=80.0, detect_sos=lambda d, c: 10.0)
    assert gap == 0.0 and reasons == []

    sos_rejected = _sos_channel_ok(df, cfg, rps_active=True, rps_slow=80.0, detect_sos=lambda d, c: None)
    assert sos_rejected is False
    gap, reasons = _diagnose_sos(cfg, df, rps_ctx, rps_slow=80.0, detect_sos=lambda d, c: None)
    assert gap > 0.0 and "未形成放量突破阻力的SOS结构" in reasons


def _ambush_rps_state(cfg: FunnelConfig, *, active: bool, fast: float | None, slow: float | None) -> Layer2RpsState:
    momentum_ok, ambush_ok = rps_filter_flags(
        cfg, active=active, rps_fast=fast, rps_slow=slow, slope_ok=True, slope_value=0.0
    )
    return Layer2RpsState(fast=fast, slow=slow, momentum_ok=momentum_ok, ambush_ok=ambush_ok)


def test_ambush_channel_and_diagnose_consistency() -> None:
    """潜伏通道：生产 ambush_channel_ok ⇔ 诊断缺口 0.0，RPS 判定必须复用生产 ambush_ok 而非只看 slow。"""
    cfg = FunnelConfig()
    close = pd.Series([10.5] * 11 + [10.0] * 20)  # 20 日跌 4.8%，贴近 MA200：形态本身满足潜伏

    def production(rps_state: Layer2RpsState) -> bool:
        return ambush_channel_ok(
            cfg, close=close, last_close=10.0, last_ma_long=10.0, rs_ok=True, rps_ok=rps_state.ambush_ok
        )

    def diagnose(rps_state: Layer2RpsState) -> tuple[float, list[str]]:
        return _diagnose_ambush(cfg, 10.0, 10.0, close, True, rps_state)

    weak_fast_strong_slow = _ambush_rps_state(cfg, active=True, fast=30.0, slow=80.0)
    assert production(weak_fast_strong_slow) is True
    assert diagnose(weak_fast_strong_slow) == (0.0, [])

    # 2026-09-17 生产 trace 里 688265 的真实读数：slow 达标但 fast 早已不"弱"，生产拒绝而旧诊断报缺口 0.0
    hot_fast = _ambush_rps_state(cfg, active=True, fast=88.0, slow=76.0)
    assert production(hot_fast) is False
    gap, reasons = diagnose(hot_fast)
    assert gap > 0.0
    assert any(reason.startswith("RPS(fast)过高") for reason in reasons)
    assert not any(reason.startswith("RPS(slow)不足") for reason in reasons)

    low_slow = _ambush_rps_state(cfg, active=True, fast=30.0, slow=50.0)
    assert production(low_slow) is False
    gap, reasons = diagnose(low_slow)
    assert gap > 0.0 and any(reason.startswith("RPS(slow)不足") for reason in reasons)

    missing = _ambush_rps_state(cfg, active=True, fast=None, slow=76.0)
    assert production(missing) is False
    assert diagnose(missing) == (0.5, ["RPS数据缺失"])

    # RPS 过滤未激活时生产放行，诊断不得再拿 slow=None 当 0 报"RPS(slow)不足"
    inactive = _ambush_rps_state(cfg, active=False, fast=None, slow=None)
    assert production(inactive) is True
    assert diagnose(inactive) == (0.0, [])
