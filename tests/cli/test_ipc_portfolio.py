"""桌面 IPC 的持仓读取。

核心是登录上下文必须传到工具层：不传的话 has_cloud() 恒为 False，已登录用户
的云端持仓永远读不到，界面静默显示本地 SQLite 缓存——可能是几天前的旧数据，
看起来像「持仓凭空变了」。这个失败没有任何报错，只能靠测试锁住。
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.ipc import methods
from cli.ipc.methods import MethodError


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


class TestPortfolioEdit:
    """手动增删改。这条路径刻意不走审批闸门，所以参数白名单是唯一防线。"""

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        sentinel = object()

        class FakeSession:
            tool_context = sentinel

        def fake_update(**kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"success": True, "message": "ok"}

        monkeypatch.setattr("agents.portfolio_tools.update_portfolio", fake_update)
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())
        seen["_sentinel"] = sentinel
        return seen

    @pytest.mark.parametrize("action", ["add", "update", "remove", "set_cash"])
    def test_allows_portfolio_actions(self, captured: dict[str, Any], action: str) -> None:
        _result("portfolio_edit", {"action": action, "code": "600519"})
        assert captured["action"] == action
        assert captured["tool_context"] is captured["_sentinel"]

    def test_rejects_delete_records(self, captured: dict[str, Any]) -> None:
        """delete_records 删的是推荐跟踪表，而且不做用户隔离——不该出现在持仓入口。"""
        with pytest.raises(MethodError) as excinfo:
            list(methods.dispatch("portfolio_edit", {"action": "delete_records", "table": "recommendation"}))
        assert "不支持的持仓操作" in str(excinfo.value)
        # 关键：拦在调用工具之前，update_portfolio 根本不该被碰到。
        assert "action" not in captured

    @pytest.mark.parametrize("action", ["", "drop", "DELETE_RECORDS", "clear"])
    def test_rejects_unknown_actions(self, captured: dict[str, Any], action: str) -> None:
        with pytest.raises(MethodError):
            list(methods.dispatch("portfolio_edit", {"action": action}))

    def test_surfaces_backend_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeSession:
            tool_context = None

        monkeypatch.setattr(
            "agents.portfolio_tools.update_portfolio",
            lambda **_kw: {"error": "shares 必须大于 0"},
        )
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())
        with pytest.raises(MethodError) as excinfo:
            list(methods.dispatch("portfolio_edit", {"action": "update", "code": "600519"}))
        assert "shares 必须大于 0" in str(excinfo.value)

    def test_partial_failure_is_not_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """批量部分失败仍带 success:True —— 只看 success 会把它报成成功。"""

        class FakeSession:
            tool_context = None

        monkeypatch.setattr(
            "agents.portfolio_tools.update_portfolio",
            lambda **_kw: {
                "success": True,
                "updated_count": 1,
                "failed_count": 2,
                "failures": ["600519 不存在", "000001 股数无效"],
            },
        )
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())
        with pytest.raises(MethodError) as excinfo:
            list(methods.dispatch("portfolio_edit", {"action": "update", "code": "600519"}))
        assert "600519 不存在" in str(excinfo.value)


class TestPortfolioSetStop:
    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        class FakeSession:
            tool_context = None

        def fake_set(**kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"success": True}

        monkeypatch.setattr("agents.portfolio_tools.set_stop_loss", fake_set)
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())
        return seen

    def test_null_clears_stop(self, captured: dict[str, Any]) -> None:
        _result("portfolio_set_stop", {"code": "600519", "stop_loss": None})
        assert captured["stop_loss"] is None

    def test_number_sets_stop(self, captured: dict[str, Any]) -> None:
        _result("portfolio_set_stop", {"code": "600519", "stop_loss": 1550.5})
        assert captured["stop_loss"] == 1550.5

    def test_missing_key_is_rejected(self, captured: dict[str, Any]) -> None:
        """漏传和显式传 null 必须区分开，否则漏传会静默清掉止损。"""
        with pytest.raises(MethodError) as excinfo:
            list(methods.dispatch("portfolio_set_stop", {"code": "600519"}))
        assert "stop_loss" in str(excinfo.value)
        assert "stop_loss" not in captured

    def test_requires_code(self, captured: dict[str, Any]) -> None:
        with pytest.raises(MethodError):
            list(methods.dispatch("portfolio_set_stop", {"stop_loss": 10}))


class TestWriteMethodsRegistered:
    @pytest.mark.parametrize("method", ["portfolio_edit", "portfolio_set_stop"])
    def test_registered(self, method: str) -> None:
        assert method in methods.METHODS


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
