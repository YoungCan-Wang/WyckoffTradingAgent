from __future__ import annotations

import json

from core.research_discovery import build_research_discovery


def test_research_inventory_keeps_discovery_when_market_blocks_ai_and_buy():
    trace = {
        "trade_date": "2026-09-04",
        "run": {"git_sha": "as-run-sha"},
        "config_digest": "frozen-digest",
        "counts": {"universe": 3},
        "market_context": {"regime": "RISK_OFF"},
        "symbols": {
            "300308": {
                "name": "中际旭创",
                "l2_eligible": True,
                "l3_eligible": False,
                "stage": "未入候选池[题材共振不足]",
                "reason": "题材/行业共振不足",
                "risk_signal": "stop_loss",
            },
            "300502": {"name": "新易盛", "l2_eligible": True, "l3_eligible": True, "stage": "买点未确认"},
            "000001": {"name": "基础淘汰", "l2_eligible": False},
        },
    }
    metrics = {
        "mainline_candidates": [{"code": "300308", "theme": "CPO", "status": "主线观察"}],
        "candidate_entries": [{"code": "600865", "entry_type": "sos", "timing": "等待次日缩量回踩"}],
        "leader_radar_rows": [{"code": "300502", "name": "新易盛"}],
    }

    result = json.loads(json.dumps(build_research_discovery(trace, metrics)))
    rows = {row["code"]: row for row in result["candidates"]}

    assert set(rows) == {"300308", "300502", "600865"}
    assert result["coverage"]["complete_trace"] is True
    assert result["source_run"] == {"git_sha": "as-run-sha"}
    assert result["counts"] == {"total": 3, "execution_blocked": 3}
    assert result["signal_counts"] == {"awaiting_confirmation": 2, "candidate_detected": 1}
    assert result["execution_counts"] == {"blocked": 3}
    assert rows["300308"]["discovery_sources"] == ["production_trace", "mainline_candidates"]
    assert rows["300308"]["signal_state"] == "awaiting_confirmation"
    assert "exit_signal:stop_loss" in rows["300308"]["blocking_reasons"]
    assert rows["600865"]["signal_state"] == "candidate_detected"
    assert rows["600865"]["confirmation_state"] == "not_evaluated"
    assert all(not row["new_buy_allowed"] and not row["direct_buy_allowed"] for row in rows.values())


def test_normal_market_research_does_not_grant_execution_or_claim_complete_trace(monkeypatch):
    monkeypatch.delenv("STEP4_BUY_BLOCK_REGIMES", raising=False)
    metrics = {
        "benchmark_context": {"regime": "NEUTRAL"},
        "total_symbols": 200,
        "candidate_entries": [{"code": "1", "entry_type": "lps"}],
        "mainline_candidates": [{"code": "2", "status": "主线观察"}],
    }

    result = build_research_discovery({}, metrics)

    assert result["coverage"]["complete_trace"] is False
    assert result["counts"] == {"total": 2, "ready_for_review": 1, "awaiting_confirmation": 1}
    assert all(row["execution_permission"] == "not_evaluated" for row in result["candidates"])
    assert all(row["direct_buy_allowed"] is False for row in result["candidates"])


def test_research_inventory_marks_degraded_data_and_keeps_missing_data_visible():
    trace = {
        "symbols": {"000001": {"stage": "数据失败", "reason": "日线拉取失败/超时"}},
        "market_context": {"regime": "NEUTRAL"},
        "data_quality": {"trade_readiness": "observe_only", "reasons": ["stale_prices"]},
    }
    result = build_research_discovery(trace, {"leader_radar_rows": [{"code": "000001"}]})

    row = result["candidates"][0]
    assert row["signal_state"] == "awaiting_confirmation"
    assert row["research_status"] == "execution_blocked"
    assert row["blocking_reasons"] == ["data_quality:stale_prices", "日线拉取失败/超时"]
