from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from workflows.funnel_data_quality import (
    FunnelDataStaleError,
    assert_funnel_data_freshness,
    build_funnel_data_quality,
    build_layer_rejections,
)


def _frame(source: str) -> pd.DataFrame:
    frame = pd.DataFrame({"close": [10.0]})
    frame.attrs["upstream_source"] = source
    return frame


def _dated_frame(day: str) -> pd.DataFrame:
    return pd.DataFrame({"date": [day], "close": [10.0]})


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
    assert result["coverage"] == {"ohlcv": 1.0, "raw_ohlcv": 1.0, "market_cap": 1.0, "financial": 1.0}
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


def test_data_quality_reports_stale_ohlcv_against_expected_trade_date() -> None:
    result = build_funnel_data_quality(
        ["000001", "000002"],
        {"000001": _dated_frame("2026-07-14"), "000002": _dated_frame("2026-07-15")},
        {"000001": 100.0, "000002": 100.0},
        {},
        financial_requested=False,
        expected_trade_date=date(2026, 7, 15),
    )

    assert result["coverage"]["fresh_ohlcv"] == 0.5
    assert "fresh_ohlcv_coverage<95%" in result["reasons"]


def test_data_quality_excludes_suspended_symbols_from_trade_readiness() -> None:
    result = build_funnel_data_quality(
        ["000001", "000002"],
        {"000001": _dated_frame("2026-07-15")},
        {"000001": 100.0, "000002": 100.0},
        {},
        financial_requested=False,
        expected_trade_date=date(2026, 7, 15),
        excluded_symbols={"000002"},
    )

    assert result["status"] == "normal"
    assert result["coverage"]["ohlcv"] == 1.0
    assert result["coverage"]["raw_ohlcv"] == 0.5
    assert result["counts"]["excluded_non_trading"] == 1


def test_data_quality_degrades_when_structural_metadata_is_missing() -> None:
    frame = _frame("2026-07-15")
    frame["turnover"] = [1.0]
    result = build_funnel_data_quality(
        ["000001"],
        {"000001": frame},
        {"000001": 100.0},
        {},
        financial_requested=False,
        sector_map={},
        concept_map={},
        turnover_expected=True,
    )

    assert "sector_coverage<90%" in result["reasons"]
    assert "concept_coverage<80%" in result["reasons"]


def test_freshness_gate_rejects_stale_benchmark_even_when_stocks_are_fresh() -> None:
    expected = date(2026, 7, 15)
    frames = {"000001": _dated_frame("2026-07-15")}

    with pytest.raises(FunnelDataStaleError, match="stale_benchmarks=\\[1\\]"):
        assert_funnel_data_freshness(
            ["000001"],
            frames,
            [_dated_frame("2026-07-15"), _dated_frame("2026-07-14")],
            expected,
        )


def test_freshness_gate_accepts_target_date_with_required_coverage() -> None:
    expected = date(2026, 7, 15)
    frames = {code: _dated_frame("2026-07-15") for code in ("000001", "000002")}

    assert_funnel_data_freshness(
        list(frames),
        frames,
        [_dated_frame("2026-07-15"), _dated_frame("2026-07-15")],
        expected,
    )


def test_freshness_gate_accepts_descending_frames_from_tushare() -> None:
    descending = pd.DataFrame(
        {
            "date": ["2026-07-15", "2026-07-14", "2026-07-10"],
            "close": [10.0, 9.8, 9.5],
        }
    )

    assert_funnel_data_freshness(
        ["000001"],
        {"000001": descending},
        [descending, descending],
        date(2026, 7, 15),
    )


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
