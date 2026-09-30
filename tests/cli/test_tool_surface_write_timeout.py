"""CLI ToolSurface must not abandon mutating calls on timeout."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.tools import ToolRegistry, _tool_surface_timeout_seconds


@pytest.mark.parametrize(
    ("name", "args", "expect_none"),
    [
        ("update_portfolio", {"action": "remove", "code": "000001"}, True),
        ("record_trade_fill", {"code": "000001", "side": "sell", "shares": 100, "price": 10.0}, True),
        ("set_stop_loss", {"code": "000001", "stop_loss": 9.5}, True),
        ("research_hypothesis", {"action": "create", "title": "t", "thesis": "th"}, True),
        ("research_hypothesis", {"action": "list"}, False),
        ("portfolio", {}, False),
        ("analyze_stock", {"code": "000001"}, False),
    ],
)
def test_tool_surface_timeout_disabled_for_mutating_calls(name, args, expect_none):
    timeout = _tool_surface_timeout_seconds(name, args, 60.0)
    assert (timeout is None) is expect_none
    if not expect_none:
        assert timeout == 60.0


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("update_portfolio", {"action": "remove", "code": "000001"}),
        ("record_trade_fill", {"code": "000001", "side": "sell", "shares": 100, "price": 10.0}),
        ("research_hypothesis", {"action": "create", "title": "t", "thesis": "th"}),
    ],
)
def test_registry_execute_disables_abandoning_timeout_for_writes(monkeypatch, name, args):
    captured: dict[str, object] = {}

    class _Surface:
        def resolve(self, tool_name):
            return object()

        def execute_tool(self, tool_name, arguments, access=None):
            captured["timeout"] = None if access is None else access.timeout_seconds
            return {"ok": True, "result": {"ok": True}, "error": None}

    registry = ToolRegistry.__new__(ToolRegistry)
    registry._tools = {name: (lambda **_kwargs: {"ok": True})}
    registry._tool_surface = _Surface()
    registry._tool_context = SimpleNamespace(state={"session_id": "s1"})
    registry._bg_manager = None
    registry._mcp_manager = None
    registry._confirm_callback = None
    registry._always_allowed = set()
    monkeypatch.setattr(registry, "_confirm_high_risk_call", lambda *_a, **_k: (args, None))
    monkeypatch.setattr(registry, "is_background", lambda *_a, **_k: False)
    monkeypatch.setattr("cli.auth.get_tool_timeout_seconds", lambda: 60.0)

    assert registry.execute(name, dict(args)) == {"ok": True}
    assert captured["timeout"] is None


def test_registry_execute_keeps_timeout_for_reads(monkeypatch):
    captured: dict[str, object] = {}

    class _Surface:
        def resolve(self, tool_name):
            return object()

        def execute_tool(self, tool_name, arguments, access=None):
            captured["timeout"] = None if access is None else access.timeout_seconds
            return {"ok": True, "result": {"ok": True}, "error": None}

    registry = ToolRegistry.__new__(ToolRegistry)
    registry._tools = {"portfolio": (lambda **_kwargs: {"ok": True})}
    registry._tool_surface = _Surface()
    registry._tool_context = SimpleNamespace(state={"session_id": "s1"})
    registry._bg_manager = None
    registry._mcp_manager = None
    registry._confirm_callback = None
    registry._always_allowed = set()
    monkeypatch.setattr(registry, "_confirm_high_risk_call", lambda *_a, **_k: ({}, None))
    monkeypatch.setattr(registry, "is_background", lambda *_a, **_k: False)
    monkeypatch.setattr("cli.auth.get_tool_timeout_seconds", lambda: 45.0)

    assert registry.execute("portfolio", {}) == {"ok": True}
    assert captured["timeout"] == 45.0
