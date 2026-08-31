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

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_clears_stop(self, captured: dict[str, Any], blank: str) -> None:
        _result("portfolio_set_stop", {"code": "600519", "stop_loss": blank})
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

    def test_rejects_non_numeric_stop(self, captured: dict[str, Any]) -> None:
        with pytest.raises(MethodError) as excinfo:
            list(methods.dispatch("portfolio_set_stop", {"code": "600519", "stop_loss": "abc"}))
        assert excinfo.value.code == "invalid_params"
        assert "stop_loss" not in captured


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


class TestIdentityMustBeAlignedBeforeWriting:
    """身份没对齐时写操作必须拒绝 —— 但**正常情况不能被拒**。

    这组测试用**真实的 DesktopSession**,不用手写替身。上一版我用了
    `sync_identity` 返回 True/False 的假会话,结果没发现真实契约完全不同:
    它返回的是 `changed`,`False` 同时表示「账号本来就一致」(最常见) 和
    「锁忙、跳过对齐」(危险)。把所有 False 当未对齐 → **所有正常的持仓写入
    都报 identity_busy**。

    替身模拟不出真实返回契约,所以这里不用替身。
    """

    def _session(self, session_user: str, disk_user: str, lock_busy: bool = False):
        """真实的 DesktopSession,只替掉磁盘读取和 registry 重建。"""
        import threading

        from cli.ipc.session import DesktopSession

        s = DesktopSession.__new__(DesktopSession)
        s._turn_lock = threading.RLock()
        s._user_id = session_user
        s._tools = object()
        s._sync_identity_locked = lambda: False
        if lock_busy:
            # 另一个线程持锁 = 对话进行中。必须在别的线程里拿,
            # RLock 对同一线程是可重入的,自己拿了测不出「锁忙」。
            ready = threading.Event()
            threading.Thread(target=lambda: (s._turn_lock.acquire(), ready.set(), None), daemon=True).start()
            ready.wait(timeout=2)
        return s, disk_user

    def _patched(self, monkeypatch, session, disk_user):
        monkeypatch.setattr("cli.ipc.session._load_session", lambda: {"user_id": disk_user})
        monkeypatch.setattr("cli.ipc.session.get_session", lambda *a, **k: session)

    def test_same_account_idle_is_allowed(self, monkeypatch) -> None:
        """最常见的情况:同账号、锁空闲。**必须放行** —— 这条是上一版的回归。"""
        from cli.ipc import methods as M

        s, disk = self._session("alice", "alice")
        self._patched(monkeypatch, s, disk)
        assert M._sync_ok(s) is True, "同账号稳定态被判成未对齐 —— 正常写入会全被拒"
        assert M._write_session() is s

    def test_same_account_while_turn_in_flight_is_allowed(self, monkeypatch) -> None:
        """锁忙但账号本来就一致 —— 也该放行。

        跳过对齐不等于身份错了:那一轮用的 registry 和磁盘上是同一个账号,
        写入是安全的。这里拒绝就变成「对话期间不能改持仓」。
        """
        from cli.ipc import methods as M

        s, disk = self._session("alice", "alice", lock_busy=True)
        self._patched(monkeypatch, s, disk)
        assert M._sync_ok(s) is True

    def test_account_changed_but_sync_skipped_is_refused(self, monkeypatch) -> None:
        """真正危险的那种:磁盘上已经换成 bob,而会话还在用 alice 的 registry。"""
        from cli.ipc import methods as M

        s, disk = self._session("alice", "bob", lock_busy=True)
        self._patched(monkeypatch, s, disk)
        assert M._sync_ok(s) is False, "身份确实错位却放行了 —— 会写到旧账号"
        with pytest.raises(M.MethodError) as excinfo:
            M._write_session()
        assert excinfo.value.code == "identity_busy"
        assert "没有执行" in excinfo.value.message

    def test_unreadable_disk_session_does_not_block(self, monkeypatch) -> None:
        """磁盘登录态读不出来时不阻断 —— 否则「文件暂时读不了」升级成「功能不可用」。"""
        from cli.ipc import methods as M

        s, _ = self._session("alice", "alice")
        monkeypatch.setattr(
            "cli.ipc.session._load_session",
            lambda: (_ for _ in ()).throw(OSError("disk gone")),
        )
        monkeypatch.setattr("cli.ipc.session.get_session", lambda *a, **k: s)
        assert M._sync_ok(s) is True

    def test_read_path_never_refuses(self, monkeypatch) -> None:
        """读路径不跟着拒绝:拒绝读会让切账号那一两秒界面整片报错。"""
        from cli.ipc import methods as M

        s, disk = self._session("alice", "bob", lock_busy=True)
        self._patched(monkeypatch, s, disk)
        assert M._synced_session() is not None

    def test_legacy_stub_without_identity_aligned_is_allowed(self, monkeypatch) -> None:
        """只有 sync_identity 的老替身视为已对齐,且仍要调它保持副作用。"""
        from cli.ipc import methods as M

        calls = []

        class Stub:
            tool_context = object()

            def sync_identity(self):
                calls.append(1)
                return False  # 老替身的返回值不该被当成安全信号

        monkeypatch.setattr("cli.ipc.session.get_session", lambda *a, **k: Stub())
        assert M._write_session() is not None
        assert calls == [1], "仍要调 sync_identity 保持原有副作用"

    def test_remote_inflight_write_refuses_after_desktop_login_swap(self, monkeypatch) -> None:
        """遥控 in-flight 写：桌面换号后不得落到新账号。

        桥 stop(wait=False) 不打断已在跑的 handler。handler 若在换号后再读
        磁盘登录态，_write_session 会对齐到 Bob 并把 Alice 手机的改动写进 Bob。
        """
        from cli.ipc import methods as M
        from cli.ipc.remote import bind_remote_request_user

        s, _ = self._session("bob", "bob")
        self._patched(monkeypatch, s, "bob")
        monkeypatch.setattr("integrations.local_auth.load_session", lambda: {"user_id": "bob"})

        with bind_remote_request_user("alice"):
            with pytest.raises(M.MethodError) as excinfo:
                M._write_session()
        assert excinfo.value.code == "identity_busy"
        assert "切换账号" in excinfo.value.message

    def test_remote_inflight_write_allows_matching_host_user(self, monkeypatch) -> None:
        from cli.ipc import methods as M
        from cli.ipc.remote import bind_remote_request_user

        s, _ = self._session("alice", "alice")
        self._patched(monkeypatch, s, "alice")
        monkeypatch.setattr("integrations.local_auth.load_session", lambda: {"user_id": "alice"})

        with bind_remote_request_user("alice"):
            assert M._write_session() is s
