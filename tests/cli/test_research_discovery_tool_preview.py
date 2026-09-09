from __future__ import annotations

import json
from pathlib import Path

from cli.tool_results import format_tool_result_for_context
from utils.tool_result_preview import tool_result_preview


def _screen_result(count: int) -> dict:
    return {
        "ok": True,
        "board": "all",
        "report_candidates": [],
        "research_discovery": {
            "counts": {"total": count, "execution_blocked": count},
            "signal_counts": {"awaiting_confirmation": count - 1, "candidate_detected": 1},
            "execution_counts": {"blocked": count},
            "coverage": {"universe": count, "decision_rows": count, "complete_trace": True},
            "candidates": [
                {
                    "code": f"{code:06d}",
                    "name": f"研究候选{code}",
                    "signal_state": "awaiting_confirmation",
                    "execution_permission": "blocked",
                    "blocking_reasons": ["market_gate:RISK_OFF"],
                    "new_buy_allowed": False,
                }
                for code in range(1, count + 1)
            ],
        },
    }


def test_screen_preview_shows_research_before_empty_ai_selection_without_expanding_all_rows():
    result = _screen_result(2400)

    preview = tool_result_preview("screen_stocks", result)

    assert len(preview) <= 2000
    assert '"total": 2400' in preview
    assert '"awaiting_confirmation": 2399' in preview
    assert '"candidate_detected": 1' in preview
    assert '"blocked": 2400' in preview
    assert '"new_buy_allowed": false' in preview
    assert '"preview_count": 3' in preview
    assert "000003" in preview
    assert "000004" not in preview
    assert "result_ref" in preview
    assert len(result["research_discovery"]["candidates"]) == 2400


def test_cli_offload_keeps_full_inventory_retrievable_and_research_counts_in_model_context(monkeypatch, tmp_path):
    monkeypatch.setattr("cli.tool_results.wyckoff_home", lambda: tmp_path)
    result = _screen_result(2400)

    message = format_tool_result_for_context("screen_stocks", "research-call", result)

    path = Path(
        next(line.removeprefix("result_ref: ") for line in message.splitlines() if line.startswith("result_ref: "))
    )
    stored = json.loads(path.read_text())
    assert len(message) < 4000
    assert '"total": 2400' in message
    assert "market_gate:RISK_OFF" in message
    assert "完整名单" in message
    assert len(stored["research_discovery"]["candidates"]) == 2400
    assert stored["research_discovery"]["candidates"][-1]["code"] == "002400"
