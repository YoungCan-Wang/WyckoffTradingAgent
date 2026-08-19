"""桌面 IPC 的持仓读取。

核心是登录上下文必须传到工具层：不传的话 has_cloud() 恒为 False，已登录用户
的云端持仓永远读不到，界面静默显示本地 SQLite 缓存——可能是几天前的旧数据，
看起来像「持仓凭空变了」。这个失败没有任何报错，只能靠测试锁住。
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.ipc import methods


def _result(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for event in methods.dispatch(name, params or {}):
        if event.get("type") == "result":
            return event
    return {}


class TestPortfolioAuthContext:
    def test_passes_session_tool_context_to_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """IPC 必须把会话上下文交给工具，而不是匿名调用。"""
        seen: dict[str, Any] = {}

        def fake_tool(mode: str = "view", tool_context: Any = None) -> dict[str, Any]:
            seen["mode"] = mode
            seen["tool_context"] = tool_context
            return {"positions": [], "free_cash": 0}

        sentinel = object()

        class FakeSession:
            tool_context = sentinel

        monkeypatch.setattr("agents.portfolio_tools.portfolio", fake_tool)
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())

        _result("portfolio")

        assert seen["mode"] == "view"
        # 关键断言：传下去的是会话真实的上下文，不是 None。
        assert seen["tool_context"] is sentinel

    def test_signed_in_context_enables_cloud_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """带 token 的上下文应让 has_cloud 为真——这是走 Supabase 的前提。"""
        from agents.tool_context import ToolContext, has_cloud

        anonymous = None
        signed_in = ToolContext(state={"user_id": "u1", "access_token": "tok"})

        assert has_cloud(anonymous) is False
        assert has_cloud(signed_in) is True

    def test_survives_missing_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """会话还没起来时不能抛异常：宁可匿名读本地，也不要整个面板报错。"""

        class NotStarted:
            tool_context = None

        captured: dict[str, Any] = {}

        def fake_tool(mode: str = "view", tool_context: Any = None) -> dict[str, Any]:
            captured["tool_context"] = tool_context
            return {"positions": [], "free_cash": 0}

        monkeypatch.setattr("agents.portfolio_tools.portfolio", fake_tool)
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: NotStarted())

        out = _result("portfolio")

        assert captured["tool_context"] is None
        assert out.get("portfolio", {}).get("positions") == []


class TestToolRegistryAccessor:
    def test_exposes_tool_context(self) -> None:
        """访问器要真实反映构造时传入的凭据，否则 IPC 拿到的是空壳。"""
        from cli.tools import ToolRegistry

        registry = ToolRegistry(user_id="u1", access_token="tok", refresh_token="ref")
        context = registry.tool_context

        assert context.state["user_id"] == "u1"
        assert context.state["access_token"] == "tok"

        from agents.tool_context import has_cloud

        assert has_cloud(context) is True
