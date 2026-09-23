from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from integrations.public_mcp.contracts import TOOL_BY_NAME, TOOLS
from integrations.public_mcp.runtime import MAX_INPUT_BYTES, MAX_RESULT_BYTES, Runtime, normalize

NAMES = {
    "query_history",
    "research_hypothesis",
    "search_stock_by_name",
    "analyze_stock",
    "get_market_overview",
    "screen_stocks",
    "run_backtest",
    "market_regime",
    "wyckoff_diagnose",
    "intraday_analysis",
    "intraday_rescue_check",
    "run_funnel_simulation",
    "portfolio",
    "update_portfolio",
    "record_trade_fill",
    "generate_ai_report",
    "generate_strategy_decision",
    "reassess_profile",
    "diagnose_backend",
}


@pytest.fixture(autouse=True)
def deny_writes(monkeypatch):
    monkeypatch.delenv("WYCKOFF_MCP_ALLOW_WRITES", raising=False)


def test_inventory_preserves_all_existing_public_tools():
    assert set(TOOL_BY_NAME) == NAMES
    assert len(TOOLS) == len(NAMES)


@pytest.mark.parametrize("spec", TOOLS, ids=lambda spec: spec.name)
def test_contract_is_valid_json_schema_and_hides_context(spec):
    descriptor = spec.descriptor()
    Draft202012Validator.check_schema(descriptor["inputSchema"])
    Draft202012Validator.check_schema(descriptor["outputSchema"])
    assert descriptor["inputSchema"]["additionalProperties"] is False
    assert "tool_context" not in descriptor["inputSchema"]["properties"]
    assert descriptor["annotations"]["idempotentHint"] is False
    assert json.loads(json.dumps(descriptor)) == descriptor


def test_discovery_without_sdk_or_domain_imports(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    probe = """
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'mcp', 'agents', 'core', 'tools', 'cli', 'workflows', 'pandas', 'supabase', 'akshare'}:
            raise AssertionError('unexpected import: ' + fullname)
sys.meta_path.insert(0, Guard())
from integrations.public_mcp.contracts import TOOLS
from integrations.public_mcp.server import create_server
from integrations.public_mcp.runtime import Runtime
import mcp_server
assert len(TOOLS) == 19
assert Runtime()._backend is None
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": root,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", probe], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "name,args",
    [
        ("not_a_tool", {}),
        ("analyze_stock", {}),
        ("analyze_stock", {"code": "000001", "tool_context": {"user_id": "other"}}),
        ("analyze_stock", {"code": "000001", "days": True}),
        ("analyze_stock", {"code": "000001", "cost": math.nan}),
        ("screen_stocks", {"limit": -1}),
        ("screen_stocks", {"limit": 3001}),
        ("market_regime", {"unexpected": 1}),
        ("market_regime", []),
        ("record_trade_fill", {"code": "000001", "side": "sell", "shares": 0, "price": 10}),
    ],
)
def test_invalid_calls_never_reach_backend(name, args):
    def backend(*_args):
        raise AssertionError("backend must not run")

    outcome = Runtime(backend).call(name, args)
    assert outcome.is_error
    assert outcome.data["code"] in ("INVALID_ARGUMENTS", "UNKNOWN_TOOL")


def test_defaults_preserve_zero_none_and_false():
    captured = []
    runtime = Runtime(lambda spec, args: captured.append(args) or {})
    assert not runtime.call("screen_stocks", {"limit": 0, "financial_metrics": False}).is_error
    assert captured[0] == {"board": "all", "limit": 0, "financial_metrics": False}
    assert not runtime.call("screen_stocks").is_error
    assert captured[1]["limit"] is None
    assert captured[1]["financial_metrics"] is None


@pytest.mark.parametrize("action", ["create", "update", "link_evidence", "evaluate", "transition"])
def test_research_mutations_are_guarded_before_backend_loading(action):
    runtime = Runtime()
    result = runtime.call("research_hypothesis", {"action": action})
    assert result.data["code"] == "WRITE_DENIED"
    assert runtime._backend is None


@pytest.mark.parametrize("action", ["list", "detail"])
def test_research_reads_are_not_blocked(action):
    result = Runtime(lambda *_: {"hypotheses": []}).call("research_hypothesis", {"action": action})
    assert not result.is_error


@pytest.mark.parametrize(
    "name,args",
    [
        ("update_portfolio", {"action": "remove", "code": "000001"}),
        ("record_trade_fill", {"code": "000001", "side": "sell", "shares": 100, "price": 10}),
    ],
)
def test_portfolio_writes_remain_denied_without_loading_backend(name, args):
    runtime = Runtime()
    result = runtime.call(name, args)
    assert result.is_error and result.data["code"] == "WRITE_DENIED"
    assert runtime._backend is None


def test_explicit_opt_in_preserves_omitted_free_cash(monkeypatch):
    monkeypatch.setenv("WYCKOFF_MCP_ALLOW_WRITES", "1")
    captured = {}

    def backend(spec, args):
        captured.update(args)
        return {"error": "set_cash 必须显式传入 free_cash"}

    result = Runtime(backend).call("update_portfolio", {"action": "set_cash"})
    assert "free_cash" in captured and captured["free_cash"] is None
    assert result.is_error


@pytest.mark.parametrize("payload", [{"error": "failure"}, {"status": "failed"}, {"success": False}])
def test_domain_errors_are_wire_errors(payload):
    assert Runtime(lambda *_: payload).call("market_regime").is_error


@pytest.mark.parametrize("payload", [[], [1, 2], False, 0, "", None])
def test_non_object_results_have_explicit_object_envelope(payload):
    result = normalize(payload)
    assert not result.is_error
    assert result.data == {"result": payload}


def test_empty_object_stays_empty_object():
    assert normalize({}).data == {}


def test_sensitive_fields_are_removed_without_mutating_backend_data():
    raw = {"nested": {"access_token": "sensitive", "api-key": "secret"}, "price": 10}
    result = normalize(raw)
    assert result.data["nested"] == {"access_token": "[REDACTED]", "api-key": "[REDACTED]"}
    assert raw["nested"]["access_token"] == "sensitive"
    assert result.data["price"] == 10


def test_exception_text_is_not_exposed_or_retried():
    called = []

    def backend(*args):
        called.append(args)
        raise RuntimeError("access_token=private-secret")

    result = Runtime(backend).call("portfolio")
    assert result.data["code"] == "TOOL_FAILED"
    assert "private-secret" not in json.dumps(result.data)
    assert len(called) == 1


def test_missing_dependency_returns_actionable_error_without_crashing():
    def backend(*_):
        raise ModuleNotFoundError("missing optional module")

    result = Runtime(backend).call("analyze_stock", {"code": "000001"})
    assert result.data["code"] == "MISSING_DEPENDENCY"


def test_limits_do_not_silently_truncate_results():
    assert normalize({"result": "x" * MAX_RESULT_BYTES}).data["code"] == "RESULT_TOO_LARGE"
    assert Runtime().call("search_stock_by_name", {"keyword": "x" * MAX_INPUT_BYTES}).data["code"] == "INPUT_TOO_LARGE"
    assert normalize({"result": float("inf")}).data["code"] == "INVALID_RESULT"


def test_arguments_and_defaults_are_not_mutated():
    supplied = {"stock_codes": ["000001"]}

    def backend(spec, args):
        args["stock_codes"].append("000002")
        return {}

    Runtime(backend).call("generate_ai_report", supplied)
    assert supplied == {"stock_codes": ["000001"]}


def test_descriptor_mutations_do_not_change_public_contract():
    spec = TOOL_BY_NAME["analyze_stock"]
    first = spec.descriptor()
    first["inputSchema"]["properties"]["mode"]["enum"].append("not-supported")
    assert "not-supported" not in spec.descriptor()["inputSchema"]["properties"]["mode"]["enum"]
