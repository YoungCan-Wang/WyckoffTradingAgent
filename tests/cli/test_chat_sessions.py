"""会话列表的存储层：标题、置顶、搜索、按账号隔离。

为什么单独一张 chat_session 表：chat_log 是 append-only 的消息明细，而标题和置顶
是整个会话的属性。塞进明细行意味着改标题要 UPDATE 每一行。workflow_run +
workflow_event 是同一形状的先例。

隔离这件事这个项目修过几次了（持仓缓存、报告目录），这里从一开始就带上 user_id：
只知道 session_id 不该能改或删别人的会话。
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """临时库。

    要同时打 LOCAL_DB_PATH **和** 复位 _conn —— get_db() 缓存连接，只改路径的话
    拿到的还是上一次那条，测试会写进用户真实的库。
    """
    import core.constants as cc
    import integrations.local_db as ldb

    logging.disable(logging.WARNING)  # 既有的 duplicate column 迁移噪音
    monkeypatch.setattr(cc, "LOCAL_DB_PATH", tmp_path / "t.db")
    ldb._conn = None
    ldb.init_db()
    yield ldb
    if ldb._conn is not None:
        ldb._conn.close()
    ldb._conn = None
    logging.disable(logging.NOTSET)


def _seed(ldb, session_id: str, user_id: str, question: str) -> None:
    ldb.save_chat_log(session_id, "user", question, user_id=user_id)
    ldb.save_chat_log(session_id, "assistant", "答复", user_id=user_id)
    ldb.upsert_chat_session(session_id, user_id, question)


def _ids(rows) -> list[str]:
    return [r["session_id"] for r in rows]


def test_title_falls_back_to_the_first_question(db):
    """没有元数据行的会话也要显示得出来 —— TUI 写的、以及迁移前的历史会话都是这样。"""
    db.save_chat_log("s1", "user", "600519 还能拿吗", user_id="alice")
    rows = db.list_chat_sessions(user_id="alice")
    assert rows[0]["title"] == "600519 还能拿吗"


def test_sessions_are_scoped_to_the_account(db):
    _seed(db, "s1", "alice", "alice 的问题")
    _seed(db, "s2", "bob", "bob 的问题")
    assert _ids(db.list_chat_sessions(user_id="alice")) == ["s1"]


def test_empty_user_id_is_the_anon_partition_not_a_wildcard(db):
    """空串是「查未登录」这个有效条件。

    用真值判断（if user_id）会让它退化成「不过滤」，于是未登录时能看到所有账号的会话。
    """
    _seed(db, "s1", "", "未登录时问的")
    _seed(db, "s2", "alice", "alice 问的")
    assert _ids(db.list_chat_sessions(user_id="")) == ["s1"]


def test_pinned_sessions_sort_first(db):
    _seed(db, "old", "alice", "较早的")
    _seed(db, "new", "alice", "较晚的")
    assert _ids(db.list_chat_sessions(user_id="alice"))[0] == "new"
    db.set_chat_session_pinned("old", True, "alice")
    assert _ids(db.list_chat_sessions(user_id="alice"))[0] == "old"


def test_pinning_does_not_look_like_new_activity(db):
    """置顶是整理动作，不该改 updated_at —— 否则一次置顶把会话伪装成刚聊过。"""
    _seed(db, "s1", "alice", "问题")
    before = db.get_db().execute("SELECT updated_at FROM chat_session WHERE session_id='s1'").fetchone()[0]
    db.set_chat_session_pinned("s1", True, "alice")
    after = db.get_db().execute("SELECT updated_at FROM chat_session WHERE session_id='s1'").fetchone()[0]
    assert before == after


def test_rename_sticks(db):
    _seed(db, "s1", "alice", "今天大盘怎么样")
    assert db.rename_chat_session("s1", "8月复盘", "alice") is True
    assert [r["title"] for r in db.list_chat_sessions(user_id="alice")] == ["8月复盘"]


def test_a_manual_title_survives_later_turns(db):
    """用户改过名之后，后续每轮的 upsert 不能把它覆盖回自动标题。"""
    _seed(db, "s1", "alice", "第一个问题")
    db.rename_chat_session("s1", "我起的名字", "alice")
    db.upsert_chat_session("s1", "alice", "第二个问题")
    assert [r["title"] for r in db.list_chat_sessions(user_id="alice")] == ["我起的名字"]


def test_blank_rename_is_rejected(db):
    """空标题会让列表里出现一个看不出是什么的条目。"""
    _seed(db, "s1", "alice", "原标题")
    assert db.rename_chat_session("s1", "   ", "alice") is False


@pytest.mark.parametrize("action", ["rename", "pin", "delete"])
def test_another_account_cannot_touch_your_sessions(db, action):
    _seed(db, "s1", "alice", "alice 的会话")
    if action == "rename":
        assert db.rename_chat_session("s1", "被改了", "bob") is False
    elif action == "pin":
        assert db.set_chat_session_pinned("s1", True, "bob") is False
    else:
        assert db.delete_chat_session("s1", "bob") == 0
    assert _ids(db.list_chat_sessions(user_id="alice")) == ["s1"]


def test_search_matches_titles_and_content(db):
    """两条路都要通：用户可能记得自己起的标题，也可能只记得聊过的内容。"""
    _seed(db, "s1", "alice", "看看这家公司")
    db.rename_chat_session("s1", "季报解读", "alice")
    _seed(db, "s2", "alice", "帮我设个止损")
    db.rename_chat_session("s2", "风控设置", "alice")
    assert _ids(db.list_chat_sessions(user_id="alice", search="季报")) == ["s1"]  # 只在标题里
    assert _ids(db.list_chat_sessions(user_id="alice", search="止损")) == ["s2"]  # 只在内容里
    assert db.list_chat_sessions(user_id="alice", search="不存在的词") == []


def test_delete_removes_messages_and_metadata(db):
    """留下孤立的 chat_session 行会让列表里出现一个点不开的空会话。"""
    _seed(db, "s1", "alice", "要删掉的")
    assert db.delete_chat_session("s1", "alice") == 2
    assert db.list_chat_sessions(user_id="alice") == []
    assert db.get_db().execute("SELECT COUNT(*) FROM chat_session WHERE session_id='s1'").fetchone()[0] == 0


def test_existing_sessions_get_backfilled_on_upgrade(db):
    """库里已经有对话了（桌面端和 TUI 都在写）。不回填的话它们在新界面里全是无标题。"""
    db.save_chat_log("old1", "user", "升级前问的问题", user_id="alice")
    db.save_chat_log("old1", "assistant", "答", user_id="alice")
    conn = db.get_db()
    conn.execute("DELETE FROM chat_session")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES(16)")
    conn.commit()
    db._conn = None
    db.init_db()
    rows = {r["session_id"]: r for r in db.get_db().execute("SELECT * FROM chat_session")}
    assert rows["old1"]["title"] == "升级前问的问题"
    assert rows["old1"]["user_id"] == "alice"


def test_injected_context_is_stripped_from_titles(db):
    """提问文本后面会被追加注入上下文，直接截断会把它显示到侧边栏。

    实测真实数据里长这样：
    `买了你司的产品怎么报销？\\n\\n[当前北京时间：2026-08-21 16:20（星期五，UTC+8）]`
    """
    db.save_chat_log("s1", "user", "怎么报销？\n\n[当前北京时间：2026-08-21 16:20（星期五，UTC+8）]", user_id="a")
    assert db.list_chat_sessions(user_id="a")[0]["title"] == "怎么报销？"


def test_a_message_that_is_only_injected_context_yields_no_title(db):
    """整条都不是用户说的话时，宁可没标题也不要显示一坨系统文本。"""
    from integrations.local_db import clean_session_title

    assert clean_session_title("[系统提示] 什么什么") == ""
    assert clean_session_title("<turn-resume-context>\n原问题: x") == ""


def test_long_titles_are_bounded(db):
    from integrations.local_db import CHAT_TITLE_MAX, clean_session_title

    assert len(clean_session_title("很长的提问" * 50)) == CHAT_TITLE_MAX


def test_listing_without_an_account_keeps_full_history(db):
    """不传 user_id 时保持原语义 —— TUI 的既有调用依赖它。"""
    _seed(db, "s1", "alice", "a")
    _seed(db, "s2", "bob", "b")
    assert len(db.list_chat_sessions()) == 2
