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
    ldb._conn = None
    ldb.init_db()
    yield tmp_path / "t.db"
    if ldb._conn is not None:
        ldb._conn.close()
    ldb._conn = None


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
