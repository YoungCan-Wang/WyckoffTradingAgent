from __future__ import annotations

import importlib
import json
import os
import sys
from copy import deepcopy
from types import ModuleType


class FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name

    def tool(self):
        return lambda func: func

    def run(self) -> None:
        return None


def import_mcp_server(monkeypatch):
    mcp_pkg = ModuleType("mcp")
    server_pkg = ModuleType("mcp.server")
    fastmcp_pkg = ModuleType("mcp.server.fastmcp")
    fastmcp_pkg.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", server_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_pkg)
    sys.modules.pop("mcp_server", None)
    return importlib.import_module("mcp_server")


def test_run_funnel_simulation_maps_main_chinext_without_mutating_env(monkeypatch):
    mcp_server = import_mcp_server(monkeypatch)
    captured_kwargs = {}

    def fake_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return (
            True,
            [{"code": "000001"}],
            {"regime": "NEUTRAL"},
            {
                "metrics": {"layer1": 1, "all_df_map": {"000001": object()}},
                "all_df_map": {"000001": object()},
            },
        )

    fake_funnel = ModuleType("workflows.wyckoff_funnel")
    fake_funnel.run = fake_run
    monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_funnel)
    monkeypatch.setenv("FUNNEL_POOL_MODE", "manual")
    monkeypatch.setenv("FUNNEL_POOL_BOARD", "chinext")
    monkeypatch.setenv("FUNNEL_EXECUTOR_MODE", "process")

    result = mcp_server.run_funnel_simulation(board="main_chinext", limit=12)

    assert result["success"] is True
    assert captured_kwargs["pool_board"] == "main_chinext_star"
    assert captured_kwargs["pool_limit_count"] == 12
    assert captured_kwargs["executor_mode"] == "thread"
    assert result["details"] == {"metrics": {"layer1": 1}}
    assert os.environ["FUNNEL_POOL_MODE"] == "manual"
    assert os.environ["FUNNEL_POOL_BOARD"] == "chinext"
    assert os.environ["FUNNEL_EXECUTOR_MODE"] == "process"


def test_run_funnel_simulation_rejects_invalid_limit_before_pipeline(monkeypatch):
    mcp_server = import_mcp_server(monkeypatch)
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return True, [], {}, {}

    fake_funnel = ModuleType("workflows.wyckoff_funnel")
    fake_funnel.run = fake_run
    monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_funnel)

    result = mcp_server.run_funnel_simulation(limit=3001)

    assert "limit 最大支持 3000" in result["error"]
    assert called is False


def test_query_history_supports_attribution_source(monkeypatch):
    mcp_server = import_mcp_server(monkeypatch)
    captured = {}

    def fake_query_history(**kwargs):
        captured.update(kwargs)
        return {
            "latest_operator_summary": "下一步=继续观察；作用范围=漏斗shadow",
            "latest_execution_state": {"scope": "funnel_shadow"},
        }

    monkeypatch.setattr(mcp_server, "_query_history", fake_query_history)

    result = mcp_server.query_history(source="attribution", limit=1)

    assert captured["source"] == "attribution"
    assert captured["limit"] == 1
    assert "作用范围=漏斗shadow" in result["latest_operator_summary"]
    assert result["latest_execution_state"]["scope"] == "funnel_shadow"


def test_research_hypothesis_maps_mcp_arguments(monkeypatch):
    mcp_server = import_mcp_server(monkeypatch)
    captured = {}

    def fake_research_hypothesis(**kwargs):
        captured.update(kwargs)
        return {"status": "created", "hypothesis": {"hypothesis_id": "hyp_1"}}

    monkeypatch.setattr(mcp_server, "_research_hypothesis", fake_research_hypothesis)

    result = mcp_server.research_hypothesis(
        action="create",
        title="Spring 样本外",
        thesis="Spring 在风险开启期具有正收益",
        invalidation_criteria="十日均值收益为负",
    )

    assert result["hypothesis"]["hypothesis_id"] == "hyp_1"
    assert captured["title"] == "Spring 样本外"
    assert captured["invalidation_criteria"] == "十日均值收益为负"


def test_mcp_screen_bounds_only_research_view_without_modifying_raw_result(monkeypatch):
    server = import_mcp_server(monkeypatch)
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
    monkeypatch.setattr(server, "_execute_mcp_tool", lambda *_a: raw)

    result = server.screen_stocks(limit=0)
    view = result["research_discovery"]

    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 150_000
    assert view["code_index_total"] == 2000
    assert set(tech_codes) <= set(view["code_index"]["awaiting_confirmation"]["blocked"])
    assert view["preview_returned"] <= 50
    assert view["preview_truncated"] is True
    assert view["full_details_location"] == "report/review_trace_when_retained"
    assert "result_ref" not in result
    assert result["action_plan"] == {"new_buy_allowed": False}
    assert result["report_candidates"] == []
    assert raw == before
    assert len(raw["research_discovery"]["candidates"]) == 2000


def test_mcp_screen_keeps_error_result_unchanged(monkeypatch):
    server = import_mcp_server(monkeypatch)
    error = {"status": "error", "error": "screen failed"}
    monkeypatch.setattr(server, "_execute_mcp_tool", lambda *_a: error)

    assert server.screen_stocks() == error
