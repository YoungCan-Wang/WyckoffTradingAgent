from __future__ import annotations

import pandas as pd

from workflows.funnel_data_quality import build_funnel_data_quality, build_layer_rejections


def _frame(source: str) -> pd.DataFrame:
    frame = pd.DataFrame({"close": [10.0]})
    frame.attrs["upstream_source"] = source
    return frame


def test_data_quality_is_ready_when_all_required_coverages_pass() -> None:
    symbols = ["000001", "000002"]

    result = build_funnel_data_quality(
        symbols,
        {"000001": _frame("tickflow"), "000002": _frame("tushare")},
        {"000001": 100.0, "000002": 80.0},
        {"000001": {"roe": 10}, "000002": {"roe": 8}},
        financial_requested=True,
    )

    assert result["status"] == "normal"
    assert result["trade_readiness"] == "ready"
    assert result["coverage"] == {"ohlcv": 1.0, "market_cap": 1.0, "financial": 1.0}
    assert result["ohlcv_source_counts"] == {"tickflow": 1, "tushare": 1}
    assert result["ohlcv_source_ratios"] == {"tickflow": 0.5, "tushare": 0.5}


def test_data_quality_degrades_when_market_cap_coverage_is_below_95_percent() -> None:
    symbols = [f"{index:06d}" for index in range(20)]
    frames = {symbol: _frame("tushare") for symbol in symbols}
    caps = {symbol: 100.0 for symbol in symbols[:18]}
    financial = {symbol: {"roe": 10} for symbol in symbols}

    result = build_funnel_data_quality(symbols, frames, caps, financial, financial_requested=True)

    assert result["status"] == "degraded"
    assert result["trade_readiness"] == "observe_only"
    assert result["coverage"]["market_cap"] == 0.9
    assert "market_cap_coverage<95%" in result["reasons"]


def test_data_quality_degrades_when_requested_financial_coverage_is_below_90_percent() -> None:
    symbols = [f"{index:06d}" for index in range(10)]
    frames = {symbol: _frame("akshare") for symbol in symbols}
    caps = {symbol: 100.0 for symbol in symbols}

    result = build_funnel_data_quality(
        symbols,
        frames,
        caps,
        {symbol: {"roe": 10} for symbol in symbols[:8]},
        financial_requested=True,
    )

    assert result["status"] == "degraded"
    assert "financial_coverage<90%" in result["reasons"]


def test_data_quality_ignores_financial_gate_when_metrics_were_not_requested() -> None:
    symbols = ["000001"]

    result = build_funnel_data_quality(
        symbols,
        {"000001": _frame("baostock")},
        {"000001": 100.0},
        {},
        financial_requested=False,
    )

    assert result["status"] == "normal"
    assert result["coverage"]["financial"] == 0.0
    assert result["financial_requested"] is False


def test_data_quality_degrades_when_ohlcv_coverage_is_below_95_percent() -> None:
    symbols = [f"{index:06d}" for index in range(20)]
    frames = {symbol: _frame("efinance") for symbol in symbols[:18]}
    caps = {symbol: 100.0 for symbol in symbols}

    result = build_funnel_data_quality(symbols, frames, caps, {}, financial_requested=False)

    assert result["status"] == "degraded"
    assert result["trade_readiness"] == "observe_only"
    assert "ohlcv_coverage<95%" in result["reasons"]


def test_layer_rejections_report_each_stage_input_pass_and_reason() -> None:
    result = build_layer_rejections(
        total_symbols=100,
        l1_symbols=[str(index) for index in range(70)],
        l2_symbols=[str(index) for index in range(30)],
        l3_symbols=[str(index) for index in range(10)],
        triggers={"sos": [("1", 80.0)], "spring": [("2", 70.0), ("1", 60.0)]},
    )

    assert result["layer1"] == {
        "input": 100,
        "passed": 70,
        "rejected": 30,
        "reason": "ST/板块/市值/价格/流动性/财务准入",
    }
    assert result["layer2"]["rejected"] == 40
    assert result["layer3"]["rejected"] == 20
    assert result["layer4"]["input"] == 10
    assert result["layer4"]["passed"] == 2
    assert result["layer4"]["rejected"] == 8
