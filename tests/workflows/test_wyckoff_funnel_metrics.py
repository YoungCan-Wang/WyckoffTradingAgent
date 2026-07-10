from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from workflows.funnel_ai_selection import FunnelAiSelection
from workflows.wyckoff_funnel import _apply_data_quality_mode, _data_quality_metrics, _layer_metrics


def _frame(source: str) -> pd.DataFrame:
    frame = pd.DataFrame({"close": [10.0]})
    frame.attrs["upstream_source"] = source
    return frame


def test_data_quality_metrics_expose_coverages_sources_and_observe_only() -> None:
    symbols = [f"{index:06d}" for index in range(20)]
    inputs = SimpleNamespace(
        pool=SimpleNamespace(symbols=symbols),
        all_df_map={symbol: _frame("tushare") for symbol in symbols[:18]},
        ref_data=SimpleNamespace(market_cap_map={symbol: 100.0 for symbol in symbols}),
        financial_map={},
        financial_metrics_requested=False,
    )

    metrics = _data_quality_metrics(inputs)

    assert metrics["data_quality"]["status"] == "degraded"
    assert metrics["data_quality_status"] == "degraded"
    assert metrics["trade_readiness"] == "observe_only"
    assert metrics["ohlcv_coverage"] == 0.9
    assert metrics["market_cap_coverage"] == 1.0
    assert metrics["financial_coverage"] == 0.0
    assert metrics["ohlcv_source_counts"] == {"tushare": 18}


def test_layer_metrics_include_rps_universe_and_stage_rejections() -> None:
    layers = SimpleNamespace(
        l1_passed=["1", "2", "3"],
        l2_passed=["1", "2"],
        l2_counts={
            "momentum": 1,
            "ambush": 0,
            "accum": 0,
            "dry_vol": 0,
            "rs_div": 0,
            "trend_cont": 1,
            "sos": 0,
        },
        l2_channel_map={"1": "主升通道", "2": "趋势延续"},
        l3_passed=["1"],
        top_sectors=["银行"],
        sector_rotation={},
        leader_radar_rows=[],
        leader_radar_symbols=[],
        mainline_candidates=[],
        mainline_ai_cap=3,
        triggers={"sos": [("1", 80.0)]},
        rps_universe_count=5,
    )

    metrics = _layer_metrics(layers, total_symbols=5)

    assert metrics["rps_universe_count"] == 5
    assert metrics["layer_rejections"]["layer1"]["rejected"] == 2
    assert metrics["layer_rejections"]["layer2"]["rejected"] == 1
    assert metrics["layer_rejections"]["layer3"]["rejected"] == 1
    assert metrics["layer_rejections"]["layer4"]["rejected"] == 0


def test_data_quality_mode_marks_selection_as_observe_only_without_dropping_shadow_candidates() -> None:
    selection = FunnelAiSelection(
        selected_for_ai=["000001"],
        trend_selected=["000001"],
        accum_selected=[],
        score_map={"000001": 88.0},
        ai_policy={},
        theme_promoted_count=0,
    )

    result = _apply_data_quality_mode(
        selection,
        {"data_quality": {"status": "degraded", "trade_readiness": "observe_only"}},
    )

    assert result.selected_for_ai == ["000001"]
    assert result.ai_policy["data_quality_status"] == "degraded"
    assert result.ai_policy["trade_readiness"] == "observe_only"
