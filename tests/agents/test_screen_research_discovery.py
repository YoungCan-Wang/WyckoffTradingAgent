from __future__ import annotations

import json
from copy import deepcopy

from agents import screen_tools
from agents.tool_context import ToolContext
from core.research_discovery import build_research_discovery


def test_agent_returns_complete_research_pool_when_no_report_candidate_is_allowed(monkeypatch):
    inventory = build_research_discovery(
        {
            "counts": {"universe": 25},
            "market_context": {"regime": "RISK_OFF"},
            "symbols": {
                f"{code:06d}": {"name": f"观察{code}", "l2_eligible": True, "stage": "买点未确认"}
                for code in range(1, 26)
            },
        },
        {},
    )
    details = {
        "metrics": {"total_symbols": 25},
        "triggers": {},
        "name_map": {},
        "trade_mode": {"regime": "RISK_OFF", "mode": "observe_only", "allow_ai_review": False},
        "research_discovery": inventory,
    }
    monkeypatch.setattr(screen_tools, "ensure_tushare_token", lambda _ctx: None)
    monkeypatch.setattr(screen_tools, "_run_funnel_with_board", lambda *_a, **_kw: (True, [], {}, details))

    result = json.loads(json.dumps(screen_tools.screen_stocks(limit=25)))

    assert "error" not in result
    assert result["report_candidates"] == []
    assert result["action_plan"]["new_buy_allowed"] is False
    assert result["research_discovery"]["counts"] == {"total": 25, "execution_blocked": 25}
    assert len(result["research_discovery"]["candidates"]) == 25
    assert all(not row["direct_buy_allowed"] for row in result["research_discovery"]["candidates"])


def test_screen_handoff_keeps_research_counts_without_copying_full_details():
    inventory = _large_inventory()
    result = {"research_discovery": inventory, "action_plan": {"new_buy_allowed": False}}
    before = deepcopy(result)
    context = ToolContext()

    screen_tools.remember_screen_handoff(context, result)
    view = context.state["last_screen_result"]["research_discovery"]

    assert view["counts"] == inventory["counts"]
    assert view["signal_counts"] == inventory["signal_counts"]
    assert view["execution_counts"] == inventory["execution_counts"]
    assert view["preview_total"] == 2000
    assert view["preview_returned"] == 3
    assert view["preview_truncated"] is True
    assert "code_index" not in view
    assert all(row["code"] in _TECH_CODES for row in view["candidates"])
    assert all("discovery_reason" not in row for row in view["candidates"])
    assert len(json.dumps(view, ensure_ascii=False).encode("utf-8")) < 10_000
    assert result == before


def test_detailed_agent_view_bounds_chinese_payload_and_preserves_complete_code_index():
    inventory = _large_inventory()
    before = deepcopy(inventory)

    view = screen_tools.research_discovery_agent_view(inventory, detailed=True)
    index = {code for group in view["code_index"].values() for codes in group.values() for code in codes}

    assert len(index) == view["code_index_total"] == 2000
    assert set(_TECH_CODES) <= index
    assert view["preview_total"] == 2000
    assert 0 < view["preview_returned"] <= 50
    assert view["preview_returned"] == len(view["candidates"])
    assert view["preview_truncated"] is True
    assert set(_TECH_CODES) <= {row["code"] for row in view["candidates"][:6]}
    assert "非收益排名" in view["preview_order"]
    assert "不改变研究池或AI选择" in view["preview_order"]
    assert view["full_details_location"] == "report/review_trace_when_retained"
    assert len(json.dumps(view, ensure_ascii=False).encode("utf-8")) < 150_000
    assert inventory == before
    view["candidates"][0]["discovery_sources"].append("mutated_preview")
    view["counts"]["total"] = 0
    assert inventory == before


def test_detailed_agent_view_retains_index_even_when_detail_exceeds_byte_budget():
    inventory = {
        "candidates": [
            {
                "code": "300308",
                "signal_state": "candidate_detected",
                "execution_permission": "blocked",
                "discovery_reason": "中文理由" * 50_000,
            },
        ]
    }

    view = screen_tools.research_discovery_agent_view(inventory, detailed=True)

    assert view["code_index"] == {"candidate_detected": {"blocked": ["300308"]}}
    assert view["preview_total"] == 1
    assert view["preview_returned"] == 0
    assert view["preview_truncated"] is True


def test_detailed_agent_view_limits_short_details_to_fifty_without_changing_order():
    inventory = {"candidates": [{"code": f"{code:06d}"} for code in range(100, 0, -1)]}

    view = screen_tools.research_discovery_agent_view(inventory, detailed=True)

    assert view["preview_returned"] == 50
    assert view["candidates"][0]["code"] == "000001"
    assert view["candidates"][-1]["code"] == "000050"
    assert inventory["candidates"][0]["code"] == "000100"


_TECH_CODES = ("300308", "300502", "002463", "002281", "300394", "603083")


def _large_inventory() -> dict:
    codes = [f"{100000 + idx:06d}" for idx in range(1994)] + list(_TECH_CODES)
    return build_research_discovery(
        {
            "counts": {"universe": len(codes)},
            "market_context": {"regime": "RISK_OFF"},
            "symbols": {
                code: {
                    "name": "科技研究样本",
                    "l2_eligible": True,
                    "stage": "买点未确认",
                    "reason": "等待主线量价确认，不允许直接买入；" * 30,
                }
                for code in codes
            },
        },
        {"mainline_candidates": [{"code": code, "theme": "光模块与通信"} for code in _TECH_CODES]},
    )
