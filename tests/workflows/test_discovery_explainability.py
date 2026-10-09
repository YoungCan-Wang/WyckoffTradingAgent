from __future__ import annotations

from types import SimpleNamespace

from core.research_discovery import build_research_discovery
from workflows.funnel_render import _research_discovery_lines, _research_source_lines


def test_discovery_sources_deduplicate_without_hiding_overlapping_blockers(monkeypatch):
    monkeypatch.setenv("STEP4_BUY_BLOCK_REGIMES", "NEUTRAL")
    trace = {
        "trade_date": "2026-09-30",
        "counts": {"universe": 2},
        "market_context": {"regime": "NEUTRAL"},
        "data_quality": {"trade_readiness": "observe_only", "reasons": ["turnover_coverage<95%"]},
        "symbols": {"000001": {"l2_eligible": True}, "000002": {"l2_eligible": True}},
    }
    metrics = {
        "mainline_candidates": [{"code": "000001"}, {"code": "000001"}, {"code": "000002"}],
        "candidate_entries": [{"code": "000001", "entry_type": "spring"}],
        "leader_radar_rows": [{"code": "000001"}],
    }
    result = build_research_discovery(trace, metrics)
    stats = result["by_source"]["mainline_candidates"]
    assert stats == {
        "total": 2,
        "signal_counts": {"candidate_detected": 1, "awaiting_confirmation": 1},
        "execution_counts": {"blocked": 2},
    }
    assert result["by_source"]["candidate_entries"]["total"] == 1
    assert result["counts"]["total"] == 2
    assert len(result["blocker_counts"]) == 2
    assert set(result["blocker_counts"].values()) == {2}
    assert sum(result["blocker_counts"].values()) == 4
    assert all(not row["new_buy_allowed"] for row in result["candidates"])
    text = "\n".join(_research_source_lines(result))
    assert "主线发现**: 2只 / 买点候选1 / 待确认1 / 拦截2" in text
    assert "turnover_coverage<95%" in text
    assert "不能相加" in text


def test_source_renderer_accepts_older_inventory_without_summary():
    result = build_research_discovery({}, {"mainline_candidates": [{"code": "000001"}]})
    result.pop("by_source")
    result.pop("blocker_counts")
    assert "主线发现**: 1只" in "\n".join(_research_source_lines(result))


def test_report_shows_trade_date_not_generation_date():
    ctx = SimpleNamespace(
        regime="NEUTRAL",
        metrics={"end_trade_date": "2026-09-30"},
        mainline_candidates=[{"code": "000001", "name": "样例", "status": "主线观察"}],
        candidate_entries=[],
        leader_radar_rows=[],
    )
    text = "\n".join(_research_discovery_lines(ctx))
    assert "行情截止 2026-09-30" in text
    assert "未评估跨日confirmed和账户OMS" in text


def test_empty_discovery_preserves_no_buy_permissions():
    result = build_research_discovery({}, {})
    assert result["by_source"] == {}
    assert result["blocker_counts"] == {}
    assert result["new_buy_allowed"] is False
