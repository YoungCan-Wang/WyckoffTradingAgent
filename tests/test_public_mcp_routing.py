from __future__ import annotations

from types import SimpleNamespace

import pytest

from integrations.public_mcp.backend import DomainBackend
from integrations.public_mcp.contracts import TOOL_BY_NAME


def _stub_backend(monkeypatch, name, handler):
    from integrations.public_mcp import backend as module
    from tools.tool_surface import ToolSurface

    actual_import = module.importlib.import_module
    spec = TOOL_BY_NAME[name]
    target_module, target_function = spec.handler.split(":")

    def loader(path, *args):
        if path == target_module:
            return SimpleNamespace(**{target_function: handler})
        return actual_import(path, *args)

    monkeypatch.setattr(module.importlib, "import_module", loader)
    backend = DomainBackend.__new__(DomainBackend)
    backend._context = SimpleNamespace(state={"user_id": "test-principal"})
    backend._surface = ToolSurface()
    return backend, spec


@pytest.mark.parametrize("name", ["portfolio", "generate_ai_report", "generate_strategy_decision", "analyze_stock"])
def test_previously_direct_calls_use_shared_surface_and_context(monkeypatch, name):
    captured = {}

    def handler(tool_context=None, **kwargs):
        captured["context"] = tool_context
        captured["args"] = kwargs
        return {"ok": True}

    backend, spec = _stub_backend(monkeypatch, name, handler)
    result = backend(spec, {"sample": 1})
    assert result == {"ok": True}
    assert captured["context"] is backend._context
    assert captured["args"] == {"sample": 1}
    assert backend._surface.resolve(name).policy.read_only == spec.read_only


@pytest.mark.parametrize(
    "name,args",
    [
        ("update_portfolio", {"action": "remove", "code": "000001"}),
        ("record_trade_fill", {"code": "000001", "side": "sell", "shares": 100, "price": 10.0}),
        ("research_hypothesis", {"action": "create", "title": "t", "thesis": "th"}),
    ],
)
def test_mutating_calls_disable_abandoning_timeout(monkeypatch, name, args):
    captured = {}

    def handler(**kwargs):
        return {"ok": True}

    def execute_tool(tool_name, arguments, access=None):
        captured["timeout"] = None if access is None else access.timeout_seconds
        return {"ok": True, "result": {"ok": True}, "error": None}

    monkeypatch.setenv("WYCKOFF_MCP_ALLOW_WRITES", "1")
    backend, spec = _stub_backend(monkeypatch, name, handler)
    backend._surface.execute_tool = execute_tool
    assert backend(spec, dict(args)) == {"ok": True}
    assert captured["timeout"] is None


def test_read_calls_keep_contract_timeout(monkeypatch):
    captured = {}

    def handler(**kwargs):
        return {"ok": True}

    def execute_tool(tool_name, arguments, access=None):
        captured["timeout"] = None if access is None else access.timeout_seconds
        return {"ok": True, "result": {"ok": True}, "error": None}

    backend, spec = _stub_backend(monkeypatch, "research_hypothesis", handler)
    backend._surface.execute_tool = execute_tool
    assert backend(spec, {"action": "list"}) == {"ok": True}
    assert captured["timeout"] == spec.timeout
