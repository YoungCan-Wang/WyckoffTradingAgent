"""会话归档：存储层筛选、账号隔离，以及 v17→v18 的升级路径。

归档和删除是两件事：归档可逆、只是从侧栏收起来；删除连着 chat_log 一起没。
这里盯住的是几个容易悄悄坏掉的点：

1. 默认列表**不能**带上已归档的 —— 侧栏是最常见的调用方
2. 归档不该改 updated_at，否则取消归档后会插到列表最前，假装刚聊过
3. 归档不该动 pinned，否则往返一次就丢了用户的置顶
4. 跨账号不能归档别人的会话
5. 老库升上来：已有的索引名不带 archived，CREATE INDEX IF NOT EXISTS 对它不生效，
   必须先 DROP 再建 —— 不重建的话默认查询会退化成全表扫
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个测试一个独立的库。HOME 改掉，避免碰到开发机上真实的会话。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from core import constants

    importlib.reload(constants)
    from integrations import local_db

    importlib.reload(local_db)
    local_db.init_db()
    return local_db


def _seed(db, session_id: str, user_id: str = "", title: str = "问句") -> None:
    conn = db.get_db()
    conn.execute(
        "INSERT INTO chat_log(session_id,user_id,role,content) VALUES(?,?,'user',?)",
        (session_id, user_id, title),
    )
    db.upsert_chat_session(session_id, user_id, title)
    conn.commit()


def _ids(db, **kw) -> list[str]:
    return sorted(r["session_id"] for r in db.list_chat_sessions(user_id="u1", **kw))


def test_default_list_excludes_archived(db):
    _seed(db, "keep", "u1")
    _seed(db, "gone", "u1")
    assert _ids(db) == ["gone", "keep"]

    db.set_chat_session_archived("gone", True, "u1")
    # 侧栏默认只看未归档 —— 归档了却还在原地，等于这个功能没生效。
    assert _ids(db) == ["keep"]
    assert _ids(db, archived=True) == ["gone"]
    assert _ids(db, archived=None) == ["gone", "keep"]


def test_archive_round_trip_restores_visibility(db):
    _seed(db, "s1", "u1")
    db.set_chat_session_archived("s1", True, "u1")
    db.set_chat_session_archived("s1", False, "u1")
    assert _ids(db) == ["s1"]


def test_archive_keeps_pinned(db):
    """归档往返不该清掉置顶 —— 那是替用户做了他没要求的决定。"""
    _seed(db, "s1", "u1")
    db.set_chat_session_pinned("s1", True, "u1")
    db.set_chat_session_archived("s1", True, "u1")
    db.set_chat_session_archived("s1", False, "u1")
    rows = db.list_chat_sessions(user_id="u1")
    assert [r["pinned"] for r in rows] == [1]


def test_archive_does_not_bump_updated_at(db):
    """归档是整理动作，不是新活动。动了 updated_at 会让它假装刚聊过。"""
    _seed(db, "s1", "u1")
    conn = db.get_db()
    before = conn.execute("SELECT updated_at FROM chat_session WHERE session_id='s1'").fetchone()[0]
    db.set_chat_session_archived("s1", True, "u1")
    after = conn.execute("SELECT updated_at FROM chat_session WHERE session_id='s1'").fetchone()[0]
    assert before == after


def test_cannot_archive_another_users_session(db):
    _seed(db, "mine", "u1")
    _seed(db, "theirs", "u2")
    assert db.set_chat_session_archived("theirs", True, "u1") is False
    # 真正的归属人可以。
    assert db.set_chat_session_archived("theirs", True, "u2") is True


def test_archived_flag_exposed_in_list(db):
    _seed(db, "s1", "u1")
    db.set_chat_session_archived("s1", True, "u1")
    row = db.list_chat_sessions(user_id="u1", archived=True)[0]
    assert row["archived"] == 1


def test_session_without_metadata_row_counts_as_unarchived(db):
    """迁移前 TUI 写的会话没有 chat_session 行。

    用 COALESCE 兜住，否则它们的 archived 是 NULL，在「未归档」和「已归档」
    两个列表里都匹配不上 —— 会话直接从界面上消失。
    """
    conn = db.get_db()
    conn.execute("INSERT INTO chat_log(session_id,user_id,role,content) VALUES('orphan','u1','user','老会话')")
    conn.commit()
    assert "orphan" in _ids(db)


def test_delete_all_removes_only_archived(db):
    """「全部删除」只碰已归档的，未归档的一根头发都不能动。"""
    _seed(db, "keep", "u1")
    _seed(db, "drop1", "u1")
    _seed(db, "drop2", "u1")
    db.set_chat_session_archived("drop1", True, "u1")
    db.set_chat_session_archived("drop2", True, "u1")

    assert db.delete_archived_chat_sessions("u1") == 2
    assert _ids(db) == ["keep"]
    assert _ids(db, archived=True) == []


def test_delete_all_also_removes_messages(db):
    """元数据和消息要一起走。只删元数据会留下一堆查不到出处的消息行。"""
    _seed(db, "gone", "u1")
    db.set_chat_session_archived("gone", True, "u1")
    db.delete_archived_chat_sessions("u1")
    conn = db.get_db()
    left = conn.execute("SELECT COUNT(*) FROM chat_log WHERE session_id='gone'").fetchone()[0]
    assert left == 0


def test_delete_all_respects_account_boundary(db):
    """别人的已归档会话不能被顺手清掉。"""
    _seed(db, "mine", "u1")
    _seed(db, "theirs", "u2")
    db.set_chat_session_archived("mine", True, "u1")
    db.set_chat_session_archived("theirs", True, "u2")

    assert db.delete_archived_chat_sessions("u1") == 1
    assert [r["session_id"] for r in db.list_chat_sessions(user_id="u2", archived=True)] == ["theirs"]


def test_delete_all_spares_sessions_without_metadata_row(db):
    """迁移前 TUI 写的会话（没有 chat_session 行）算未归档，不能被清掉。

    这条盯的是一个具体的写法错误：如果按「不在未归档列表里」来删，
    这些没有元数据行的老会话会被一起干掉。
    """
    conn = db.get_db()
    conn.execute("INSERT INTO chat_log(session_id,user_id,role,content) VALUES('legacy','u1','user','老会话')")
    conn.commit()
    _seed(db, "archived", "u1")
    db.set_chat_session_archived("archived", True, "u1")

    db.delete_archived_chat_sessions("u1")
    assert "legacy" in _ids(db)


def test_delete_all_on_empty_is_zero_not_error(db):
    """没有已归档时点一下不该炸，也不该谎报删了东西。"""
    _seed(db, "keep", "u1")
    assert db.delete_archived_chat_sessions("u1") == 0
    assert _ids(db) == ["keep"]


def test_ipc_delete_archived_reports_count_and_landing_session(db, monkeypatch):
    """IPC 层：报真实个数，并且给一个能写的落脚会话。"""
    from cli.ipc import methods as ipc_methods

    # 钉住当前账号。`_current_user_id()` 读的是真实登录态，全量跑的时候别的
    # 测试可能留下一个已登录 session —— 那时它返回真实 user_id，和这里种的
    # 空串分区对不上，归档会报「会话不存在」。单独跑却是通的，正是这个原因。
    monkeypatch.setattr(ipc_methods, "_current_user_id", lambda: "")
    METHODS = ipc_methods.METHODS

    def run(method, params=None):
        return list(METHODS[method](params or {}))[-1]

    _seed(db, "a", "")
    _seed(db, "b", "")
    _seed(db, "keep", "")
    run("chat_archive", {"session_id": "a", "archived": True})
    run("chat_archive", {"session_id": "b", "archived": True})

    out = run("chat_delete_archived")
    assert out["deleted"] == 2
    # 落脚会话必须有，否则下一轮对话没有能写的 id。
    assert out["session_id"]
    remaining = [s["session_id"] for s in run("chat_sessions", {})["sessions"]]
    assert remaining == ["keep"]
    # 再点一次：0，而不是报错。
    assert run("chat_delete_archived")["deleted"] == 0


def test_upgrade_from_v17_adds_column_and_rebuilds_index(tmp_path, monkeypatch):
    """老库升上来：补列，并且把索引重建成带 archived 的版本。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from core import constants

    importlib.reload(constants)
    path = constants.LOCAL_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version VALUES(17);
        CREATE TABLE chat_log(session_id TEXT, user_id TEXT DEFAULT '', role TEXT,
            content TEXT, created_at TEXT DEFAULT (datetime('now')),
            tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0, error TEXT DEFAULT '',
            model TEXT DEFAULT '', elapsed_s REAL DEFAULT 0, metadata TEXT DEFAULT '');
        CREATE TABLE chat_session(session_id TEXT PRIMARY KEY, user_id TEXT DEFAULT '',
            title TEXT DEFAULT '', pinned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')));
        CREATE INDEX idx_chatsess_user ON chat_session(user_id, pinned DESC, updated_at DESC);
        INSERT INTO chat_log(session_id,user_id,role,content) VALUES('old','u1','user','升级前');
        INSERT INTO chat_session(session_id,user_id,title,pinned) VALUES('old','u1','升级前',1);
        """
    )
    conn.commit()
    conn.close()

    from integrations import local_db

    importlib.reload(local_db)
    local_db.init_db()
    conn = local_db.get_db()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_session)")}
    assert "archived" in cols

    index_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_chatsess_user'").fetchone()[0]
    assert "archived" in index_sql

    # 升级后侧栏看到的东西和升级前一致：老会话仍在，且仍然置顶。
    rows = local_db.list_chat_sessions(user_id="u1")
    assert [(r["session_id"], r["pinned"], r["archived"]) for r in rows] == [("old", 1, 0)]
