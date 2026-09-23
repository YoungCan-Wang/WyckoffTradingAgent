from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from types import ModuleType, SimpleNamespace

from integrations.public_mcp.backend import DomainBackend
from integrations.public_mcp.contracts import TOOL_BY_NAME
from integrations.public_mcp.handlers import run_funnel_simulation
from integrations.public_mcp.runtime import Runtime


def test_run_funnel_simulation_maps_main_chinext_without_mutating_env(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return (
            True,
            [{"code": "000001"}],
            {"regime": "NEUTRAL"},
            {
                "metrics": {"layer1": 1, "all_df_map": {"000001": object()}},
                "all_df_map": {"000001": object()},
            },
        )

    module = ModuleType("workflows.wyckoff_funnel")
    module.run = fake_run
    monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", module)
    monkeypatch.setenv("FUNNEL_POOL_MODE", "manual")
    monkeypatch.setenv("FUNNEL_POOL_BOARD", "chinext")
    monkeypatch.setenv("FUNNEL_EXECUTOR_MODE", "process")
    result = run_funnel_simulation(board="main_chinext", limit=12)
    assert result["success"] is True
    assert captured["pool_board"] == "main_chinext_star"
    assert captured["pool_limit_count"] == 12
    assert captured["executor_mode"] == "thread"
    assert captured["notify"] is False
    assert result["details"] == {"metrics": {"layer1": 1}}
    assert os.environ["FUNNEL_POOL_MODE"] == "manual"
    assert os.environ["FUNNEL_POOL_BOARD"] == "chinext"
    assert os.environ["FUNNEL_EXECUTOR_MODE"] == "process"


def test_run_funnel_simulation_rejects_invalid_limit_before_pipeline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pipeline must not execute")

    module = ModuleType("workflows.wyckoff_funnel")
    module.run = forbidden
    monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", module)
    assert "error" in run_funnel_simulation(limit=3001)
    assert "error" in run_funnel_simulation(limit=True)


def test_query_history_supports_attribution_source():
    captured = {}

    def backend(spec, args):
        assert spec.handler == "agents.history_tools:query_history"
        captured.update(args)
        return {"latest_execution_state": {"scope": "funnel_shadow"}}

    result = Runtime(backend).call("query_history", {"source": "attribution", "limit": 1})
    assert captured["source"] == "attribution" and captured["limit"] == 1
    assert result.data["latest_execution_state"]["scope"] == "funnel_shadow"


def test_research_hypothesis_maps_mcp_arguments(monkeypatch):
    monkeypatch.setenv("WYCKOFF_MCP_ALLOW_WRITES", "1")
    captured = {}

    def backend(spec, args):
        assert spec.handler == "agents.research_tools:research_hypothesis"
        captured.update(args)
        return {"status": "created", "hypothesis": {"hypothesis_id": "hyp_1"}}

    result = Runtime(backend).call(
        "research_hypothesis",
        {
            "action": "create",
            "title": "Spring 样本外",
            "thesis": "待验证假设",
            "invalidation_criteria": "十日均值收益为负",
        },
    )
    assert not result.is_error
    assert result.data["hypothesis"]["hypothesis_id"] == "hyp_1"
    assert captured["title"] == "Spring 样本外"
    assert captured["invalidation_criteria"] == "十日均值收益为负"


def screen_backend(monkeypatch, raw):
    from agents import screen_tools
    from tools.tool_surface import ToolSurface

    monkeypatch.setattr(screen_tools, "screen_stocks", lambda **kwargs: raw)
    backend = DomainBackend.__new__(DomainBackend)
    backend._context = SimpleNamespace(state={})
    backend._surface = ToolSurface()
    return backend(TOOL_BY_NAME["screen_stocks"], {"limit": 0})


def test_mcp_screen_bounds_only_research_view_without_modifying_raw_result(monkeypatch):
    tech_codes = ("300308", "300502", "002463", "002281", "300394", "603083")
    codes = [f"{100000 + idx:06d}" for idx in range(1994)] + list(tech_codes)
    raw = {
        "action_plan": {"new_buy_allowed": False},
        "report_candidates": [],
        "research_discovery": {
            "counts": {"total": 2000, "execution_blocked": 2000},
            "signal_counts": {"awaiting_confirmation": 2000},
            "execution_counts": {"blocked": 2000},
            "candidates": [
                {
                    "code": code,
                    "name": "科技观察",
                    "signal_state": "awaiting_confirmation",
                    "execution_permission": "blocked",
                    "discovery_reason": "不可直接买入，等待确认；" * 30,
                }
                for code in codes
            ],
        },
    }
    before = deepcopy(raw)
    result = screen_backend(monkeypatch, raw)
    view = result["research_discovery"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 150_000
    assert view["code_index_total"] == 2000
    assert set(tech_codes) <= set(view["code_index"]["awaiting_confirmation"]["blocked"])
    assert view["preview_returned"] <= 50 and view["preview_truncated"] is True
    assert view["full_details_location"] == "report/review_trace_when_retained"
    assert "result_ref" not in result
    assert result["action_plan"] == {"new_buy_allowed": False}
    assert result["report_candidates"] == []
    assert raw == before and len(raw["research_discovery"]["candidates"]) == 2000


def test_mcp_screen_keeps_error_result_unchanged(monkeypatch):
    error = {"status": "error", "error": "screen failed"}
    assert screen_backend(monkeypatch, error) == error
