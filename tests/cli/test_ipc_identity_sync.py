"""按账号取数的 IPC 方法都必须先把身份对齐到磁盘上的登录态。

ToolRegistry 是会话 start() 时建的，而磁盘上的登录态可能之后变了（换账号登录）。
读路径 portfolio() 一直有 sync_identity()，注释里也解释了风险；但两条**写**路径
（portfolio_edit / portfolio_set_stop）漏了 —— 常驻会话换账号后，写操作会把新
账号的改动落到旧账号的云端。读路径严防的「张冠李戴」，写路径反而没设防，而写
的后果更严重且完全无声。

tracking / attribution 同样漏了（评审没提，是核查时一起发现的）。

根因是那段对齐逻辑原本内联在 portfolio() 的函数体里、没有名字，复制不过去就等于
忘掉。现在收敛成 _synced_session()，这组测试锁住「每条路径都走它」。
"""

from __future__ import annotations

import pytest

from cli.ipc import methods


class FakeSession:
    """记录 sync_identity 是否在读 tool_context 之前被调用过。"""

    def __init__(self) -> None:
        self.synced = 0
        self._context_reads: list[int] = []

    def sync_identity(self) -> None:
        self.synced += 1

    def run_turn(self, text: str):
        yield {"type": "done", "text": text, "synced": self.synced}

    @property
    def tool_context(self):
        # 记录每次读取时已经同步过几次 —— 0 就说明顺序错了
        self._context_reads.append(self.synced)
        return {"fake": True}

    @property
    def context_reads(self) -> list[int]:
        return self._context_reads

    user_id = "user-new"


@pytest.fixture
def fake_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr("cli.ipc.session.get_session", lambda: session)
    return session


def test_synced_session_calls_sync_first(fake_session):
    result = methods._synced_session()
    assert fake_session.synced == 1
    assert result is fake_session


def test_synced_session_tolerates_a_session_without_sync(monkeypatch):
    """会话还没起来（或测试替身）时不该整个面板报错，宁可退回匿名结果。"""

    class Bare:
        tool_context = {}

    monkeypatch.setattr("cli.ipc.session.get_session", lambda: Bare())
    assert methods._synced_session() is not None


@pytest.mark.parametrize(
    "method_name, params",
    [
        ("portfolio_edit", {"action": "set_cash", "free_cash": 100}),
        ("portfolio_set_stop", {"code": "600519", "stop_loss": 10.0}),
        ("portfolio_set_stop", {"code": "600519", "stop_loss": None}),
    ],
)
def test_write_paths_sync_before_using_tool_context(fake_session, monkeypatch, method_name, params):
    """写路径读 tool_context 之前必须已经同步过。

    这是这组测试的核心：不是「有没有调 sync」，而是**顺序**。先读 context 再
    同步等于没同步 —— 那次写已经带着旧账号的 registry 发出去了。
    """
    monkeypatch.setattr(
        "agents.portfolio_tools.update_portfolio",
        lambda **kwargs: {"success": True, "failed_count": 0},
    )
    monkeypatch.setattr(
        "agents.portfolio_tools.set_stop_loss",
        lambda **kwargs: {"success": True, "failed_count": 0},
    )

    list(methods.METHODS[method_name](params))

    assert fake_session.synced >= 1, f"{method_name} 没有对齐账号"
    assert fake_session.context_reads, f"{method_name} 没有读 tool_context？用例假设失效"
    assert all(seen >= 1 for seen in fake_session.context_reads), (
        f"{method_name} 在同步之前就读了 tool_context —— 那次写会落到旧账号"
    )


def test_read_path_still_syncs(fake_session, monkeypatch):
    """别在重构里把读路径原有的防护弄丢。"""
    monkeypatch.setattr("agents.portfolio_tools.portfolio", lambda **kwargs: {"positions": []})
    list(methods.METHODS["portfolio"]({}))
    assert fake_session.synced >= 1
    assert all(seen >= 1 for seen in fake_session.context_reads)


def test_chat_syncs_identity_before_running_turn(fake_session):
    events = list(methods.METHODS["chat"]({"text": "hello"}))

    assert fake_session.synced == 1
    assert events == [{"type": "done", "text": "hello", "synced": 1}]


def test_no_per_account_method_reads_tool_context_unsynced():
    """穷举：源码里不该再有绕过 _synced_session 的 tool_context 取用。

    比逐个方法写断言更耐久 —— 新增按账号取数的方法时，作者不必记得来加测试，
    忘了对齐这条就会红。
    """
    import inspect

    src = inspect.getsource(methods)
    offenders = [line.strip() for line in src.splitlines() if "tool_context=get_session()" in line]
    assert offenders == [], f"这些地方绕过了 _synced_session(): {offenders}"


def test_sync_identity_does_not_block_on_an_in_flight_turn(monkeypatch):
    """对话进行中不该被身份对齐挂住。

    run_turn 持 _turn_lock 贯穿整个流式输出（可能几分钟）。如果 sync_identity
    无条件等锁，一次读持仓就会挂到对话结束，而它期间不发任何事件 —— 直接撞上
    python-bridge 的静默超时，表现为「读持仓卡死」。

    这条锁住「拿不到锁就跳过」：宁可晚一次对齐，不要卡住一个读请求。
    """
    import threading

    from cli.ipc.session import DesktopSession

    session = DesktopSession()
    session._user_id = "old-user"
    # 模拟另一个线程正在跑一轮对话
    session._turn_lock.acquire()
    try:
        done = threading.Event()
        result: list[bool] = []

        def call_sync():
            result.append(session.sync_identity())
            done.set()

        worker = threading.Thread(target=call_sync, daemon=True)
        worker.start()
        # 立刻返回，不是等到锁释放
        assert done.wait(timeout=2.0), "sync_identity 被进行中的对话挂住了"
        assert result == [False], "拿不到锁时应返回 False（跳过重建）"
        # 关键：身份没被改动，那一轮仍然用它起始的账号跑完
        assert session._user_id == "old-user"
    finally:
        session._turn_lock.release()


def test_sync_identity_still_rebuilds_when_no_turn_is_running(monkeypatch, tmp_path):
    """别把上一条的「跳过」写成了「永不对齐」。"""
    from cli.ipc import session as session_mod

    monkeypatch.setattr(
        session_mod, "_load_session", lambda: {"user_id": "new-user", "access_token": "", "refresh_token": ""}
    )

    class FakeRegistry:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_provider(self, _p): ...
        def set_confirm_callback(self, _c): ...
        def set_ask_user_question_callback(self, _c): ...
        def set_mcp_manager(self, _m): ...

    monkeypatch.setattr("cli.tools.ToolRegistry", FakeRegistry)

    s = session_mod.DesktopSession()
    s._user_id = "old-user"
    s._messages = [{"role": "user", "content": "上一个账号的话"}]
    assert s.sync_identity() is True
    assert s._user_id == "new-user"
    assert s._messages == [], "换人了，上一个账号的历史不能留下"
