"""桌面端的对话留存。

原来 cli/ipc/session.py 是纯内存：不建 scratchpad、不写 chat_log。用户在
Electron 里聊了几十轮，误关窗口就全没了 —— 而 TUI 用户不会，因为 TUI 两样都建。
三端共用同一套 runtime，却只有一端有留存，这是接线漏了而不是设计选择。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """把库指到临时文件。

    要同时打 LOCAL_DB_PATH **和** 复位 _conn —— get_db() 缓存连接，只改路径
    的话拿到的还是上一次那条连接，测试会写进用户真实的 ~/.wyckoff/wyckoff.db
    （我第一版就是这样，三个测试互相看见对方的数据）。用完再复位，避免污染后续。
    """
    import core.constants as cc
    import integrations.local_db as ldb

    monkeypatch.setattr(cc, "LOCAL_DB_PATH", tmp_path / "t.db")
    ldb.reset_connection()
    ldb.init_db()
    yield tmp_path / "t.db"
    ldb.reset_connection()


def test_chat_log_has_a_user_id_column(db):
    """对话按账号隔离。少了这列，两个账号的对话会混在同一张表里 ——
    和之前修过的「持仓缓存/报告按账号分区」是同一类问题。"""
    from integrations.local_db import get_db

    cols = {r["name"] for r in get_db().execute("PRAGMA table_info(chat_log)").fetchall()}
    assert "user_id" in cols


def test_saved_rows_carry_the_account(db):
    from integrations.local_db import load_chat_logs, save_chat_log

    save_chat_log("s1", "user", "你好", user_id="alice")
    rows = load_chat_logs(session_id="s1")
    assert rows[0]["user_id"] == "alice"


def test_load_can_filter_by_account(db):
    from integrations.local_db import load_chat_logs, save_chat_log

    save_chat_log("s1", "user", "alice 的问题", user_id="alice")
    save_chat_log("s2", "user", "bob 的问题", user_id="bob")
    assert [r["content"] for r in load_chat_logs(user_id="alice")] == ["alice 的问题"]


def test_empty_user_id_queries_the_anon_partition(db):
    """空字符串是有意义的查询（未登录），不是「不过滤」。

    用真值判断（if user_id）会把它误当成不过滤，于是未登录用户能看到所有人的记录。
    """
    from integrations.local_db import load_chat_logs, save_chat_log

    save_chat_log("s1", "user", "未登录时问的", user_id="")
    save_chat_log("s2", "user", "alice 问的", user_id="alice")
    assert [r["content"] for r in load_chat_logs(user_id="")] == ["未登录时问的"]


def test_no_user_id_argument_keeps_full_history(db):
    """不传 user_id 时保持原语义 —— TUI 与既有调用方依赖它。"""
    from integrations.local_db import load_chat_logs, save_chat_log

    save_chat_log("s1", "user", "a", user_id="alice")
    save_chat_log("s2", "user", "b", user_id="bob")
    assert len(load_chat_logs()) == 2


def test_desktop_session_wires_persistence():
    """桌面端要建 scratchpad、写 chat_log，并把 scratchpad 交给 runtime。"""
    import inspect

    from cli.ipc.session import DesktopSession

    src = inspect.getsource(DesktopSession)
    assert "_chatlog_save" in src
    assert "AgentRuntime(self._provider, self._tools, scratchpad=" in src, "scratchpad 没传给 runtime"
    assert "_session_id" in src, "没有会话标识就无法把多轮归到一次会话"


def test_persistence_failure_does_not_break_the_answer():
    """留存是附加价值。库锁了、盘满了，不该让用户拿不到回答。"""
    import inspect

    from cli.ipc.session import DesktopSession

    src = inspect.getsource(DesktopSession._chatlog_save)
    assert "except Exception" in src


class TestEmptyReplyStillPersists:
    """以确认收尾的那一轮常常没有正文，但它照样是一次对话。

    原来 _persist_reply 整段挂在 `if reply:` 下面。模型发起确认时往往一个字都不写
    （它在等工具结果），于是 user 行进了库、assistant 行没有、标题也没生成 ——
    侧边栏里那条会话看着是空的。用户的原话是「会话历史没有保存，我找不到会话了」。
    """

    def _persist(self, db, reply, *, session_id="s1"):
        from cli.ipc.session import DesktopSession
        from integrations.local_db import get_db

        session = DesktopSession(session_id)
        session._chatlog_save("user", "把 600519 的止损挪到 1200")
        session._persist_reply("把 600519 的止损挪到 1200", {"type": "done", "text": reply})
        return (
            get_db()
            .execute(
                "SELECT role, content FROM chat_log WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
            .fetchall()
        )

    def test_empty_reply_writes_an_assistant_row(self, db):
        rows = self._persist(db, "")
        assert [r["role"] for r in rows] == ["user", "assistant"], "只有 user 行，会话在列表里就是空的"

    def test_session_shows_up_in_the_list(self, db):
        """真正的症状是这个：列表里找不到,而不是某一行缺了。"""
        from integrations.local_db import list_chat_sessions

        self._persist(db, "", session_id="s-empty")
        assert "s-empty" in {s["session_id"] for s in list_chat_sessions()}

    def test_non_empty_reply_still_saved(self, db):
        rows = self._persist(db, "已改好")
        assert [r["content"] for r in rows][1] == "已改好"

    def test_empty_reply_not_appended_to_history(self, db):
        """落盘要,但别往模型上下文里塞一条空的 assistant 消息 ——
        有的 provider 会直接拒掉空内容的消息。"""
        from cli.ipc.session import DesktopSession

        session = DesktopSession("s2")
        before = len(session._messages)
        session._persist_reply("问题", {"type": "done", "text": ""})
        assert len(session._messages) == before

    def test_empty_reply_gets_no_title(self, db):
        """空回复概括不出东西。_maybe_title 该自己跳过，而不是起个空标题或者报错。"""
        import inspect

        from cli.ipc.session import DesktopSession

        src = inspect.getsource(DesktopSession._maybe_title)
        assert "if not user_text.strip() or not reply.strip():" in src
        assert "return" in src
