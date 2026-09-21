"""Tests for Scheme A market regime gate shadow evaluation."""

from __future__ import annotations

import pandas as pd

from core.layer2_strength import (
    BenchmarkContext,
    build_benchmark_context,
    eval_scheme_a_shadow_gate,
)
from core.market_trade_mode import resolve_market_trade_mode
from workflows.daily_job_persistence import _tracking_symbol


def _make_bench_df(prices: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"trade_date": dates, "close": prices, "pct_chg": [0.0] * len(prices)})


def test_eval_scheme_a_shadow_gate_empty() -> None:
    res = eval_scheme_a_shadow_gate(None)
    assert res["action"] == "UNKNOWN"
    assert res["passed"] is True
    assert res["reason"] == "基准数据缺失"


def test_eval_scheme_a_shadow_gate_short_series() -> None:
    df = _make_bench_df([100.0] * 10)
    res = eval_scheme_a_shadow_gate(df, ma_w=20)
    assert res["action"] == "UNKNOWN"
    assert res["passed"] is True
    assert "样本不足" in res["reason"]


def test_eval_scheme_a_shadow_gate_allows_when_above_hurdle() -> None:
    # 20 days at 100.0, MA20 = 100.0, 1% hurdle = 101.0
    # Day 21 at 102.0 -> above 101.0 -> ALLOW
    prices = [100.0] * 20 + [102.0]
    df = _make_bench_df(prices)
    res = eval_scheme_a_shadow_gate(df, ma_w=20, buffer_pct=0.01)
    assert res["action"] == "ALLOW"
    assert res["passed"] is True
    assert res["close"] == 102.0
    assert res["distance_pct"] > 0
    assert "高于" in res["reason"]


def test_eval_scheme_a_shadow_gate_blocks_when_below_hurdle() -> None:
    # 20 days at 100.0, MA20 = 100.0, 1% hurdle = 101.0
    # Day 21 at 100.5 -> below 101.0 -> BLOCK
    prices = [100.0] * 20 + [100.5]
    df = _make_bench_df(prices)
    res = eval_scheme_a_shadow_gate(df, ma_w=20, buffer_pct=0.01)
    assert res["action"] == "BLOCK"
    assert res["passed"] is False
    assert res["close"] == 100.5
    assert res["distance_pct"] < 0
    assert "低于" in res["reason"]


def test_build_benchmark_context_includes_shadow_gate() -> None:
    class DummyCfg:
        enable_market_regime_gate = False
        market_regime_gate_ma = 20
        market_regime_gate_buffer_pct = 0.01
        bench_drop_days = 3
        bench_drop_threshold = -3.0

    bench_df = _make_bench_df([100.0] * 25)
    # 1. When shadow_bench_df is None, shadow action is UNKNOWN (avoids mislabeling 000001 as 000985)
    ctx_default = build_benchmark_context(
        bench_df,
        DummyCfg(),
        sort_frame=lambda df: df,
        latest_trade_date=lambda df: df["trade_date"].iloc[-1],
    )
    assert isinstance(ctx_default, BenchmarkContext)
    assert ctx_default.regime_gate_passed is True  # production gate remains open (False config)
    assert ctx_default.regime_gate_shadow["action"] == "UNKNOWN"
    assert ctx_default.regime_gate_shadow["reason"] == "基准数据缺失"

    # 2. When shadow_bench_df is explicitly provided, evaluates Scheme A shadow gate
    shadow_df = _make_bench_df([100.0] * 20 + [102.0])
    ctx_with_shadow = build_benchmark_context(
        bench_df,
        DummyCfg(),
        sort_frame=lambda df: df,
        latest_trade_date=lambda df: df["trade_date"].iloc[-1],
        shadow_bench_df=shadow_df,
    )
    assert ctx_with_shadow.regime_gate_shadow["action"] == "ALLOW"
    assert ctx_with_shadow.regime_gate_shadow["bench_code"] == "000985"


def test_tracking_symbol_attaches_shadow_gate() -> None:
    item = {"code": "000001", "name": "平安银行", "candidate_status": "confirmed"}
    trade_mode = resolve_market_trade_mode("BULL")
    benchmark_context = {
        "regime": "BULL",
        "market_regime_gate_shadow": {
            "bench_code": "000985",
            "action": "BLOCK",
            "close": 4100.0,
            "threshold": 4150.0,
            "distance_pct": -1.2,
            "reason": "000985 close=4100.00 低于 门控阈值4150.00 (-1.20%)",
        },
    }
    row = _tracking_symbol(item, trade_mode, benchmark_context=benchmark_context)
    assert row["shadow_gate_action"] == "BLOCK"
    assert "低于" in row["shadow_gate_detail"]
    assert row["candidate_metrics"]["regime_gate_shadow"]["action"] == "BLOCK"
    assert row["candidate_metrics"]["regime_gate_shadow"]["distance_pct"] == -1.2
