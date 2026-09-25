from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.public_mcp_backend import DomainBackend
from integrations.public_mcp.contracts import TOOL_BY_NAME


def _stub_backend(monkeypatch, name, handler):
    from agents import public_mcp_backend as module
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


@pytest.mark.parametrize(
    "name,args",
    [
        ("update_portfolio", {"action": "remove", "code": "000001"}),
        ("record_trade_fill", {"code": "000001", "side": "sell", "shares": 100, "price": 10.0}),
        ("research_hypothesis", {"action": "create", "title": "test", "thesis": "test"}),
    ],
)
def test_runtime_keeps_write_lock_after_caller_stops_waiting(monkeypatch, name, args):
    import threading
    from dataclasses import replace

    from integrations.public_mcp import runtime as module

    first_entered, second_attempted, second_entered, release = (threading.Event() for _ in range(4))
    calls, outcomes = [], []

    def handler(**kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            first_entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release first write")
        else:
            second_entered.set()
        return {"ok": True}

    monkeypatch.setenv("WYCKOFF_MCP_ALLOW_WRITES", "1")
    backend, spec = _stub_backend(monkeypatch, name, handler)
    monkeypatch.setattr(module, "TOOL_BY_NAME", {name: replace(spec, timeout=0.02)})
    runtime = module.Runtime(backend)

    def call_second():
        second_attempted.set()
        outcomes.append(runtime.call(name, dict(args)))

    first = threading.Thread(target=lambda: outcomes.append(runtime.call(name, dict(args))))
    second = threading.Thread(target=call_second)
    try:
        first.start()
        assert first_entered.wait(1)
        first.join(timeout=0.05)
        second.start()
        assert second_attempted.wait(1)
        assert not second_entered.wait(0.15)
        assert first.is_alive()
    finally:
        release.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 2 and all(not result.is_error for result in outcomes)
    assert calls == [first.ident, second.ident]
