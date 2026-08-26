"""桌面端会话注册表:多会话共存、共享工具环境、从库里恢复。

原来是单例 + 「新分析」直接清空 _messages —— 旧对话既不在界面上也回不来。
现在是 dict[session_id, DesktopSession]。

两个关键约束：
1. 工具环境（provider / ToolRegistry / MCP）必须共享 —— 建一套约 6 秒，
   每个会话建一份的话新建会话就是 6 秒白屏。
2. 因此审批回调每轮都要重绑：registry 只有一个 _confirm_callback 字段
   （cli/tools.py:845），不重绑的话待审批操作会记到错的会话上。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def registry(monkeypatch):
    """空注册表 + 假的工具环境。

    不真跑 start()：那要 6 秒（MCP + pandas + supabase），而这些测试关心的是
    注册表语义，不是启动过程。
    """
    import cli.ipc.session as S

    monkeypatch.setattr(S, "_sessions", {})
    monkeypatch.setattr(S, "_active_session_id", "")

    def fake_start(self):
        self._owns_env = True
        self._tools = object()
        self._provider = object()
        self._mcp_manager = None
        self._user_id = "alice"

    monkeypatch.setattr(S.DesktopSession, "start", fake_start)
    return S


def test_first_session_builds_the_environment(registry):
    s = registry.get_session()
    assert s._owns_env is True
    assert s.session_id in registry._sessions


def test_new_sessions_share_the_environment(registry):
    """建一套要 6 秒。新建会话必须是即时的，所以复用而不是重建。"""
    a = registry.get_session()
    b = registry.new_session()
    assert b._tools is a._tools
    assert b._provider is a._provider
    assert b.session_id != a.session_id


def test_only_the_owner_stops_shared_mcp(registry):
    """shutdown 会遍历所有会话调 stop()。每个都停一次会掐掉别人在用的连接。"""
    a = registry.get_session()
    stopped: list[int] = []
    a._mcp_manager = type("M", (), {"stop": lambda _self: stopped.append(1)})()
    b = registry.new_session()
    b.stop()
    assert stopped == [], "非拥有者不该停共享 MCP"
    a.stop()
    assert stopped == [1]


def test_new_session_does_not_wipe_the_old_one(registry):
    """这正是要修的行为：原来「新分析」调 reset() 把当前对话擦掉。"""
    a = registry.get_session()
    a._messages.append({"role": "user", "content": "旧对话"})
    registry.new_session()
    assert a._messages == [{"role": "user", "content": "旧对话"}]


def test_switching_returns_the_same_live_session(registry):
    a = registry.get_session()
    a._messages.append({"role": "user", "content": "x"})
    registry.new_session()
    assert registry.get_session(a.session_id) is a


def test_switching_updates_the_active_session(registry):
    a = registry.get_session()
    b = registry.new_session()
    assert registry.active_session_id() == b.session_id
    registry.get_session(a.session_id)
    assert registry.active_session_id() == a.session_id


def test_an_unknown_id_is_revived_from_storage(registry, monkeypatch):
    """从列表里点开一个进程重启后才存在于数据库的会话。"""
    registry.get_session()
    loaded: list[str] = []
    monkeypatch.setattr(
        registry.DesktopSession,
        "load_history",
        lambda self: loaded.append(self.session_id) or 0,
    )
    revived = registry.get_session("from-db-123")
    assert revived.session_id == "from-db-123"
    assert loaded == ["from-db-123"], "必须尝试从库里读历史"
    assert revived._tools is not None, "复活的会话也要有工具环境"


def test_live_sessions_are_bounded(registry):
    """内存里不能无限攒。淘汰掉的历史都在 chat_log 里，再点开会重新读。"""
    registry.get_session()
    for _ in range(registry.MAX_LIVE_SESSIONS + 5):
        registry.new_session()
    assert len(registry._sessions) <= registry.MAX_LIVE_SESSIONS


def test_pruning_keeps_the_active_session(registry):
    registry.get_session()
    for _ in range(registry.MAX_LIVE_SESSIONS + 3):
        latest = registry.new_session()
    assert latest.session_id in registry._sessions


def test_dropping_a_session_moves_the_active_pointer(registry):
    a = registry.get_session()
    b = registry.new_session()
    registry.drop_session(b.session_id)
    assert b.session_id not in registry._sessions
    assert registry.active_session_id() == a.session_id


def test_identity_change_clears_every_session(registry, monkeypatch):
    """换账号时**每个**会话的历史都不能留给新账号。

    只清当前会话的话，切过去就能看到上一个账号的对话。
    """
    a = registry.get_session()
    b = registry.new_session()
    a._messages.append({"role": "user", "content": "alice 的话"})
    b._messages.append({"role": "user", "content": "alice 的另一句"})
    monkeypatch.setattr(
        registry,
        "_load_session",
        lambda: {"user_id": "bob", "access_token": "t", "refresh_token": "r"},
    )
    assert registry.sync_all_identities() is True
    assert a._messages == []
    assert b._messages == []


def test_identity_change_keeps_the_environment_shared(registry, monkeypatch):
    """重建 registry 之后各会话不能各持一份 —— 那等于回到 6 秒问题。"""
    a = registry.get_session()
    b = registry.new_session()
    monkeypatch.setattr(
        registry,
        "_load_session",
        lambda: {"user_id": "bob", "access_token": "t", "refresh_token": "r"},
    )
    registry.sync_all_identities()
    assert a._tools is b._tools
    assert a._user_id == b._user_id == "bob"


def test_same_account_is_not_a_change(registry, monkeypatch):
    monkeypatch.setattr(registry, "_load_session", lambda: {"user_id": "alice"})
    registry.get_session()
    assert registry.sync_all_identities() is False


def test_shutdown_empties_the_registry(registry):
    registry.get_session()
    registry.new_session()
    registry.shutdown_session()
    assert registry._sessions == {}
    assert registry.active_session_id() == ""


class TestResumeIsAccountScoped:
    """恢复历史会话必须按账号过滤,否则跨账号泄漏到**模型上下文**里。

    这条是复审发现的 P1。`load_history` 原来只按 session_id 查:

        rows = load_chat_logs(session_id=self._session_id, limit=400)

    前端切账号后如果 sessionId 没清(那是另一个 bug),新账号会带着旧 session_id
    过来,于是上一个账号的对话被装进模型上下文。UI 那侧的账号过滤挡不住它 ——
    泄漏发生在模型输入里,界面上根本看不见。

    写入侧(save_chat_log)一直带 user_id,只有这条恢复路径漏了。
    """

    def test_resume_skips_other_accounts_rows(self, registry, monkeypatch) -> None:
        seen: dict = {}

        def fake_load(*, session_id=None, limit=200, user_id=None):
            seen.update(session_id=session_id, user_id=user_id)
            # 模拟库里只有 alice 的记录:按 bob 过滤应该拿到空
            if user_id == "alice":
                return [{"role": "user", "content": "Alice 的机密持仓分析"}]
            return []

        monkeypatch.setattr("integrations.local_db.load_chat_logs", fake_load)

        session = registry.DesktopSession("shared-sid")
        session.start()
        session._user_id = "bob"  # 当前登录的是 bob
        restored = session.load_history()

        assert seen["user_id"] == "bob", f"没有按账号过滤,实际传了 {seen['user_id']!r}"
        assert restored == 0, "拿到了别人的历史 —— 那会进模型上下文"

    def test_resume_still_works_for_own_rows(self, registry, monkeypatch) -> None:
        """过滤不能把自己的历史也挡掉 —— 那是另一种坏法。"""
        monkeypatch.setattr(
            "integrations.local_db.load_chat_logs",
            lambda *, session_id=None, limit=200, user_id=None: (
                [{"role": "user", "content": "我自己的对话"}] if user_id == "alice" else []
            ),
        )
        session = registry.DesktopSession("my-sid")
        session.start()  # fake_start 把 _user_id 设成 alice
        assert session.load_history() > 0, "自己的历史应该恢复得回来"
