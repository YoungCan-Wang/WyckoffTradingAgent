"""会话管理的 IPC 方法。

七个方法:chat（带 session_id）、chat_reset（改成开新会话）、chat_sessions、
chat_load、chat_delete、chat_rename、chat_pin。

preload 和 main.js 不用动 —— `py:call` 是通用透传，方法名不在白名单里。
"""

from __future__ import annotations

import logging

import pytest

from cli.ipc import methods


def _result(method: str, **params):
    events = list(methods.dispatch(method, params))
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"{method} 没有返回 result 事件"
    return results[-1]


class FakeSession:
    """替身。真会话要 start()（约 4 秒 MCP + pandas），这些测试只关心方法契约。"""

    def __init__(self, session_id: str = "live-1") -> None:
        self._session_id = session_id
        self._messages: list[dict] = []
        self.user_id = "alice"

    @property
    def session_id(self) -> str:
        return self._session_id

    def sync_identity(self) -> bool:
        return False

    def run_turn(self, text: str):
        yield {"type": "done", "text": text}


@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.constants as cc
    import integrations.local_db as ldb

    logging.disable(logging.WARNING)
    monkeypatch.setattr(cc, "LOCAL_DB_PATH", tmp_path / "t.db")
    ldb._conn = None
    ldb.init_db()
    yield ldb
    if ldb._conn is not None:
        ldb._conn.close()
    ldb._conn = None
    logging.disable(logging.NOTSET)


@pytest.fixture
def session(monkeypatch):
    """把会话层换成替身，并让 _current_user_id 稳定返回 alice。"""
    import cli.ipc.session as S

    fake = FakeSession()
    created: list[str] = []

    def fake_get(session_id: str = ""):
        if session_id:
            fake._session_id = session_id
        return fake

    def fake_new():
        fake._session_id = f"new-{len(created) + 1}"
        created.append(fake._session_id)
        return fake

    monkeypatch.setattr(S, "get_session", fake_get)
    monkeypatch.setattr(S, "new_session", fake_new)
    monkeypatch.setattr(S, "active_session_id", lambda: fake._session_id)
    monkeypatch.setattr(S, "drop_session", lambda _sid: None)
    monkeypatch.setattr(methods, "_current_user_id", lambda: "alice")
    fake.created = created
    return fake


def _seed(ldb, session_id: str, question: str, user_id: str = "alice") -> None:
    ldb.save_chat_log(session_id, "user", question, user_id=user_id)
    ldb.save_chat_log(session_id, "assistant", "答复", user_id=user_id)
    ldb.upsert_chat_session(session_id, user_id, question)


def test_reset_opens_a_new_session_instead_of_wiping(db, session):
    """语义变了:原来是清空当前会话（旧对话就没了），现在开新的。"""
    out = _result("chat_reset")
    assert out["reset"] is True
    assert out["session_id"] == "new-1"
    assert session.created == ["new-1"]


def test_chat_reports_which_session_the_turn_belongs_to(db, session):
    """流式输出期间用户可能切走，前端要知道这一轮归谁。"""
    events = list(methods.dispatch("chat", {"text": "你好", "session_id": "s7"}))
    first = next(e for e in events if e.get("type") == "result")
    assert first["session_id"] == "s7"


def test_chat_needs_text(db, session):
    with pytest.raises(methods.MethodError) as err:
        list(methods.dispatch("chat", {}))
    assert err.value.code == "invalid_params"


def test_sessions_are_listed_for_the_current_account(db, session):
    _seed(db, "s1", "alice 的会话")
    _seed(db, "s2", "bob 的会话", user_id="bob")
    out = _result("chat_sessions")
    assert [s["session_id"] for s in out["sessions"]] == ["s1"]


def test_listing_reports_the_active_session(db, session):
    _seed(db, "s1", "问题")
    assert _result("chat_sessions")["active"] == session.session_id


def test_listing_supports_search(db, session):
    _seed(db, "s1", "看看茅台")
    _seed(db, "s2", "看看银行")
    out = _result("chat_sessions", search="茅台")
    assert [s["session_id"] for s in out["sessions"]] == ["s1"]


def test_load_returns_turns_for_rendering(db, session):
    _seed(db, "s1", "我的持仓怎么了")
    out = _result("chat_load", session_id="s1")
    assert out["session_id"] == "s1"
    assert [t["role"] for t in out["turns"]] == ["user", "assistant"]
    assert out["turns"][0]["content"] == "我的持仓怎么了"


def test_load_rejects_another_account_the_same_way_as_missing(db, session):
    """不区分「不存在」和「不属于你」—— 区分就等于确认这个 id 存在。"""
    _seed(db, "s1", "bob 的会话", user_id="bob")
    with pytest.raises(methods.MethodError) as err:
        list(methods.dispatch("chat_load", {"session_id": "s1"}))
    assert err.value.code == "not_found"

    with pytest.raises(methods.MethodError) as err2:
        list(methods.dispatch("chat_load", {"session_id": "never-existed"}))
    assert err2.value.code == "not_found"


def test_delete_removes_and_provides_a_landing_session(db, session):
    """删掉当前会话后必须有落脚处，否则下一轮会写进一个刚被删掉的 id。"""
    _seed(db, "s1", "要删的")
    out = _result("chat_delete", session_id="s1")
    assert out["deleted"] == 2
    assert out["session_id"], "删除后要给出一个可用的会话 id"
    assert db.list_chat_sessions(user_id="alice") == []


def test_delete_does_not_touch_other_accounts(db, session):
    _seed(db, "s1", "bob 的", user_id="bob")
    assert _result("chat_delete", session_id="s1")["deleted"] == 0
    assert len(db.list_chat_sessions(user_id="bob")) == 1


def test_rename_and_pin(db, session):
    _seed(db, "s1", "原标题")
    assert _result("chat_rename", session_id="s1", title="新标题")["renamed"] is True
    assert _result("chat_pin", session_id="s1", pinned=True)["pinned"] is True
    row = db.list_chat_sessions(user_id="alice")[0]
    assert row["title"] == "新标题"
    assert row["pinned"] == 1


@pytest.mark.parametrize(
    "method,params",
    [
        ("chat_load", {}),
        ("chat_delete", {}),
        ("chat_rename", {"session_id": "s1"}),
        ("chat_rename", {"title": "x"}),
        ("chat_pin", {}),
    ],
)
def test_missing_params_are_rejected(db, session, method, params):
    with pytest.raises(methods.MethodError) as err:
        list(methods.dispatch(method, params))
    assert err.value.code == "invalid_params"


@pytest.mark.parametrize("method", ["chat_rename", "chat_pin"])
def test_acting_on_another_account_session_is_not_found(db, session, method):
    _seed(db, "s1", "bob 的", user_id="bob")
    params = {"session_id": "s1", "title": "改名", "pinned": True}
    with pytest.raises(methods.MethodError) as err:
        list(methods.dispatch(method, params))
    assert err.value.code == "not_found"


def test_all_methods_are_registered():
    for name in ("chat_sessions", "chat_load", "chat_delete", "chat_rename", "chat_pin"):
        assert name in methods.METHODS, f"{name} 没进 METHODS，前端调不到"
