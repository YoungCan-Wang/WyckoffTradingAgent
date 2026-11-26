from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import agents.screen_tools as screen_tools
import workflows.wyckoff_funnel as wyckoff_funnel
from core.research_discovery import build_research_discovery
from core.wyckoff_engine import FunnelConfig
from integrations.fetch_a_share_csv import TradingWindow
from tests.core.test_slow_bull_engine import _frame_from_closes, _stage_b_fixture
from workflows.funnel_ai_selection import FunnelAiSelection
from workflows.funnel_render import render_legacy_funnel_card, render_modern_funnel_card
from workflows.funnel_render_context import build_render_context
from workflows.review_trace import REVIEW_TRACE_PER_ROW_FIELDS, REVIEW_TRACE_ROW_FIELDS, build_review_trace


def _fake_funnel_data(monkeypatch):
    df_600887 = _stage_b_fixture(last_volume=1_100_000.0, pullback=True)
    df_watch = _stage_b_fixture(last_volume=2_400_000.0, pullback=False)
    csi = _frame_from_closes([4000.0] * len(df_600887))
    fin = {"roe": 14.0, "period_end": "2026-09-30", "debt_to_asset_ratio": 42.0, "operating_cash_to_revenue": 15.0}
    monkeypatch.setattr(wyckoff_funnel, "MAIN_BENCH_CODE", "000300")
    monkeypatch.setattr(
        "integrations.market_metadata.fetch_slow_bull_daily_basic_map",
        lambda **_k: {
            "600887": {"market_cap_yi": 2124.0, "dv_ttm_pct": 4.1, "turnover_pct": 1.1},
            "600025": {"market_cap_yi": 2150.0, "dv_ttm_pct": 2.1, "turnover_pct": 0.9},
        },
    )
    return SimpleNamespace(
        window=TradingWindow(date(2026, 1, 1), date(2026, 11, 25)),
        bench_df=csi,
        smallcap_df=csi,
        all_df_map={"600887": df_600887, "600025": df_watch},
        ref_data=SimpleNamespace(
            name_map={"600887": "伊利股份", "600025": "华能水电"},
            market_cap_map={"600887": 2124.0, "600025": 2150.0},
            sector_map={"600887": "食品饮料", "600025": "水力发电"},
            financial_map={"600887": fin, "600025": fin},
        ),
    )


def test_build_slow_bull_pool_populates_metrics_cleanly(monkeypatch) -> None:
    data = _fake_funnel_data(monkeypatch)

    payload, candidates = wyckoff_funnel._build_slow_bull_pool(data)

    assert payload["research_only"] is True
    assert payload["new_buy_allowed"] is False
    assert payload["counts"]["pullback_count"] == 1
    assert payload["counts"]["watch_count"] == 1
    assert [row["code"] for row in payload["pullback_candidates"]] == ["600887"]
    assert {row["code"] for row in candidates} == {"600887", "600025"}


def test_review_trace_and_research_discovery_include_slow_bull_under_risk_off(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    data = _fake_funnel_data(monkeypatch)
    slow_bull_payload, slow_bull_candidates = wyckoff_funnel._build_slow_bull_pool(data)

    inputs = SimpleNamespace(
        cfg=FunnelConfig(),
        window=data.window,
        pool=SimpleNamespace(symbols=["600887", "600025"]),
        ref_data=data.ref_data,
        all_df_map=data.all_df_map,
        layers=SimpleNamespace(
            l1_passed=["600887", "600025"],
            l2_passed=[],
            l3_passed=[],
            l2_channel_map={},
            l2_rejections={"600887": "rps_low", "600025": "rps_low"},
            rps_fast_map={"600887": 62.0, "600025": 58.0},
            rps_slow_map={"600887": 45.0, "600025": 42.0},
        ),
        candidates=SimpleNamespace(candidate_entries=[], exit_signals={}, exit_evaluated=[], l3_score_map={}),
    )
    metrics = {
        "benchmark_context": {"regime": "RISK_OFF"},
        "trade_mode": "blocked",
        "slow_bull": slow_bull_payload,
        "slow_bull_candidates": slow_bull_candidates,
    }
    trace = build_review_trace(inputs, {"spring": [], "evr": [], "trend_pullback": []}, metrics)

    row_keys = set(trace["symbols"]["600887"].keys())
    assert "slow_bull" in trace["symbols"]["600887"]
    assert row_keys - REVIEW_TRACE_PER_ROW_FIELDS == set(REVIEW_TRACE_ROW_FIELDS)
    assert trace["slow_bull_summary"]["counts"]["pullback_count"] == 1

    discovery = build_research_discovery(trace, metrics)
    assert "slow_bull_candidates" in discovery["by_source"]
    by_code = {row["code"]: row for row in discovery["candidates"]}
    assert by_code["600887"]["signal_state"] == "candidate_detected"
    assert by_code["600887"]["new_buy_allowed"] is False


def test_modern_and_legacy_funnel_cards_render_slow_bull_under_blocked_gate(monkeypatch) -> None:
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "UNKNOWN,NEUTRAL,RISK_OFF,CRASH,BLACK_SWAN")
    data = _fake_funnel_data(monkeypatch)
    slow_bull_payload, slow_bull_candidates = wyckoff_funnel._build_slow_bull_pool(data)

    monkeypatch.setattr("workflows.funnel_render_context.load_stock_name_map", lambda: data.ref_data.name_map)
    monkeypatch.setattr("workflows.funnel_render_context.fetch_sector_map", lambda: data.ref_data.sector_map)

    metrics = {
        "total_symbols": 2,
        "pool_main": 2,
        "pool_chinext": 0,
        "pool_star": 0,
        "pool_bse": 0,
        "pool_merged": 2,
        "pool_st_excluded": 0,
        "pool_limit": 0,
        "pool_batches": 1,
        "layer1": 2,
        "layer2": 0,
        "layer3": 0,
        "total_hits": 0,
        "benchmark_context": {"regime": "RISK_OFF", "close": 3800.0, "ma50": 3900.0, "ma200": 3950.0},
        "end_trade_date": "2026-11-25",
        "slow_bull": slow_bull_payload,
        "slow_bull_candidates": slow_bull_candidates,
        "data_quality": {"status": "normal", "trade_readiness": "ready", "reasons": [], "coverage": {}},
    }
    ctx = build_render_context({"spring": [], "evr": [], "trend_pullback": []}, metrics)
    empty_sel = FunnelAiSelection(
        selected_for_ai=[],
        trend_selected=[],
        accum_selected=[],
        score_map={},
        ai_policy={
            "quota_family": "RISK_OFF",
            "requested_trend_quota": 0,
            "requested_accum_quota": 0,
            "trend_quota": 0,
            "accum_quota": 0,
            "total_cap": 0,
            "max_trend_l3_fill": 0,
            "max_accum_l3_fill": 0,
        },
        theme_promoted_count=0,
    )

    modern = render_modern_funnel_card(ctx, empty_sel, return_details=True)
    legacy = render_legacy_funnel_card(ctx, empty_sel, return_details=False)

    for text in (modern.content, legacy.content):
        assert "【🐢 中线机构慢牛底仓（20~60日观察）】" in text
        assert "600887 伊利股份" in text
        assert "600025 华能水电" in text
        assert "-7%硬止损" in text
    assert modern.details is not None
    assert modern.details["slow_bull"]["counts"]["pullback_count"] == 1


def test_screen_stocks_exposes_parallel_slow_bull_view_and_handoff(monkeypatch) -> None:
    data = _fake_funnel_data(monkeypatch)
    slow_bull_payload, _ = wyckoff_funnel._build_slow_bull_pool(data)
    details = {
        "metrics": {"total_symbols": 2, "layer1": 2, "layer2": 0, "layer3": 0, "slow_bull": slow_bull_payload},
        "slow_bull": slow_bull_payload,
        "triggers": {},
        "trade_mode": {"regime": "NEUTRAL", "mode": "execution_blocked", "allow_recommendation_write": False},
        "research_discovery": {"counts": {"total": 1}, "signal_counts": {}, "execution_counts": {}, "candidates": []},
    }

    res = screen_tools._build_screen_result(
        ok=True,
        board="main",
        style=None,
        theme=None,
        include_financial_metrics=True,
        symbols=[],
        details=details,
    )
    assert res["slow_bull"]["research_only"] is True
    assert res["slow_bull"]["new_buy_allowed"] is False
    assert [row["code"] for row in res["slow_bull"]["pullback_candidates"]] == ["600887"]

    from agents.tool_context import ToolContext

    ctx = ToolContext(state={})
    screen_tools.remember_screen_handoff(ctx, res)
    assert ctx.state["last_screen_result"]["slow_bull"]["counts"]["pullback_count"] == 1
