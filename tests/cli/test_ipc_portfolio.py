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


class TestSharesMustBeExact:
    """
    小数股数要明确拒绝，不能静默截断。

    原来是 int(params["shares"])：输入 1.9 会被悄悄写成 1。改掉用户填的数字比
    报错危险得多 —— 他以为记的是 1.9，账面是 1，对不上账时也查不出哪一步丢的。
    """

    @pytest.mark.parametrize("given", [1.9, 0.5, "2.5", -1.5, 100.0001])
    def test_fractional_is_rejected(self, given: Any) -> None:
        with pytest.raises(MethodError) as excinfo:
            list(methods.dispatch("portfolio_edit", {"action": "add", "code": "600519", "shares": given}))
        assert excinfo.value.code == "invalid_params"
        assert "整数" in excinfo.value.message

    @pytest.mark.parametrize(("given", "expected"), [(100, 100), ("200", 200), (3.0, 3), ("4.0", 4)])
    def test_integral_values_pass_through(self, given: Any, expected: int) -> None:
        """3.0 是整数，只是写成了浮点 —— 这种该放过去。"""
        from cli.ipc.methods import _exact_shares

        assert _exact_shares(given) == expected

    @pytest.mark.parametrize("given", ["abc", "1e", {}, []])
    def test_non_numeric_is_rejected(self, given: Any) -> None:
        from cli.ipc.methods import _exact_shares

        with pytest.raises(MethodError):
            _exact_shares(given)

    @pytest.mark.parametrize("given", [None, "", 0])
    def test_missing_becomes_zero(self, given: Any) -> None:
        """没填就是 0，交给下游按各自的 action 规则校验。"""
        from cli.ipc.methods import _exact_shares

        assert _exact_shares(given) == 0


class TestIdentitySync:
    """
    account 每次读磁盘，而 ToolRegistry 是 start() 时用当时的 token 建的。
    两者分叉时 portfolio 返回的是**上一个账号**的持仓，却会被前端当成当前账号
    的数据缓存起来 —— 比不隔离更糟，因为看起来是隔离好的。
    """

    def test_sync_identity_rebuilds_on_account_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cli.ipc import session as S

        sess = S.DesktopSession()
        sess._user_id = "user-A"
        sess._tools = object()
        sess._messages = [{"role": "user", "content": "A 的对话"}]
        monkeypatch.setattr(S, "_load_session", lambda: {"user_id": "user-B", "access_token": "tokB"})

        assert sess.sync_identity() is True
        assert sess.user_id == "user-B"
        # 换人了，上一个账号的历史不能留给新账号
        assert sess._messages == []

    def test_sync_identity_is_noop_when_same(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """身份没变就别重建：ToolRegistry 带着待审批队列和 MCP 连接。"""
        from cli.ipc import session as S

        sess = S.DesktopSession()
        sess._user_id = "user-A"
        sentinel = object()
        sess._tools = sentinel
        sess._messages = [{"role": "user", "content": "保留"}]
        monkeypatch.setattr(S, "_load_session", lambda: {"user_id": "user-A", "access_token": "tokA"})

        assert sess.sync_identity() is False
        assert sess._tools is sentinel
        assert sess._messages != []

    def test_portfolio_reports_account_it_actually_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        前端拿这个 user_id 当缓存 key。不回传的话它只能用 account 问来的账号，
        那正是「key 写着 B、内容是 A」的成因。
        """
        calls: list[str] = []

        class FakeSession:
            tool_context = None
            user_id = "user-A"

            def sync_identity(self) -> bool:
                calls.append("synced")
                return False

        monkeypatch.setattr(
            "agents.portfolio_tools.portfolio",
            lambda mode="view", tool_context=None: {"positions": [], "free_cash": 0},
        )
        monkeypatch.setattr("cli.ipc.session.get_session", lambda: FakeSession())

        out = _result("portfolio")

        assert calls == ["synced"], "读持仓前必须先对齐身份"
        assert out["user_id"] == "user-A"
