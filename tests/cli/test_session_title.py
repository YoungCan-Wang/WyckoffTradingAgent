"""会话标题生成。

为什么不直接用首条提问：提问常常是「我的持仓怎么了」这种口语，同一个人一周能问
五遍，列表里全是重样的条目。让模型看过问答之后概括一句才带上「聊了什么」的信息。

为什么在后台线程：实测那次模型调用要 19 秒（推理模型）。挂在 run_turn 里等于每轮
对话都多等这么久，而标题只影响侧边栏好不好认。
"""

from __future__ import annotations

import logging

import pytest


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


class FakeProvider:
    """返回固定标题的替身。真调用要十几秒，且不该在测试里打网络。"""

    def __init__(self, text: str = "贵州茅台买点研判") -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools, system_prompt=""):
        self.calls.append(messages)
        self.prompt = system_prompt
        return {"text": self.text}


@pytest.fixture
def session(monkeypatch):
    import cli.ipc.session as S

    s = S.DesktopSession("sess-1")
    s._provider = FakeProvider()
    s._user_id = "alice"
    return s


def test_title_comes_from_the_model(session):
    assert session._ask_for_title("600519 能买吗", "结构未破") == "贵州茅台买点研判"


def test_prompt_asks_for_a_specific_subject(session):
    """不加这条约束模型会回「持仓分析」这种谁都适用的空话。"""
    session._ask_for_title("600519 能买吗", "结构未破")
    assert "不要写成" in session._provider.prompt


def test_quotes_and_trailing_punctuation_are_stripped(session):
    session._provider = FakeProvider('"贵州茅台买点研判。"')
    assert session._ask_for_title("x", "y") == "贵州茅台买点研判"


def test_only_the_first_line_is_kept(session):
    session._provider = FakeProvider("贵州茅台买点研判\n（附：仅供参考）")
    assert session._ask_for_title("x", "y") == "贵州茅台买点研判"


def test_long_input_is_truncated_before_sending(session):
    """标题只需要知道聊的是什么，把整篇分析发过去纯属浪费 token。"""
    session._ask_for_title("问" * 500, "答" * 900)
    body = session._provider.calls[0][0]["content"]
    assert len(body) < 700


def test_no_provider_yields_no_title(session):
    session._provider = None
    assert session._ask_for_title("x", "y") == ""


def test_generation_runs_off_the_turn(session, monkeypatch):
    """实测模型要 19 秒。同步做等于每轮对话都慢那么久。"""
    started: list[str] = []
    monkeypatch.setattr(
        "threading.Thread",
        lambda **kw: type("T", (), {"start": lambda _s: started.append(kw.get("name", ""))})(),
    )
    session._maybe_title("问题", "回复")
    assert started == ["chat-title"]


@pytest.mark.parametrize("user_text,reply", [("", "回复"), ("问题", ""), ("  ", "  ")])
def test_empty_turns_are_skipped(session, monkeypatch, user_text, reply):
    calls: list[int] = []
    monkeypatch.setattr("threading.Thread", lambda **_kw: calls.append(1))
    session._maybe_title(user_text, reply)
    assert calls == []


def test_worker_titles_the_first_turn(db, session):
    db.save_chat_log("sess-1", "user", "600519 能买吗", user_id="alice")
    db.save_chat_log("sess-1", "assistant", "结构未破", user_id="alice")
    db.upsert_chat_session("sess-1", "alice", "600519 能买吗")
    session._title_worker("sess-1", "alice", "600519 能买吗", "结构未破")
    assert db.list_chat_sessions(user_id="alice")[0]["title"] == "贵州茅台买点研判"


def test_worker_leaves_later_turns_alone(db, session):
    """第二轮之后会话已经有名字了，再改会让用户眼前的条目突然变名字。"""
    for i in range(3):
        db.save_chat_log("sess-1", "user", f"问题{i}", user_id="alice")
        db.save_chat_log("sess-1", "assistant", f"回复{i}", user_id="alice")
    db.upsert_chat_session("sess-1", "alice", "问题0")
    session._title_worker("sess-1", "alice", "问题2", "回复2")
    assert db.list_chat_sessions(user_id="alice")[0]["title"] == "问题0"


def test_a_rename_during_generation_wins(db, session):
    """模型要十几秒才回，这期间用户改了名字就不能被覆盖。

    这是真实竞态而不是理论问题：19 秒足够用户看一眼列表并顺手改个名。
    """

    class SlowProvider:
        """在「模型调用期间」模拟用户改名。"""

        def chat(self, *_a, **_k):
            db.rename_chat_session("sess-1", "我起的名字", "alice")
            return {"text": "贵州茅台买点研判"}

    session._provider = SlowProvider()
    db.save_chat_log("sess-1", "user", "600519 能买吗", user_id="alice")
    db.save_chat_log("sess-1", "assistant", "结构未破", user_id="alice")
    db.upsert_chat_session("sess-1", "alice", "600519 能买吗")
    session._title_worker("sess-1", "alice", "600519 能买吗", "结构未破")
    assert [s["title"] for s in db.list_chat_sessions(user_id="alice")] == ["我起的名字"]


def test_worker_failure_is_swallowed(db, session):
    """标题是锦上添花。模型挂了就留着首条提问当标题。"""

    class Boom:
        def chat(self, *_a, **_k):
            raise RuntimeError("模型挂了")

    session._provider = Boom()
    db.save_chat_log("sess-1", "user", "原提问", user_id="alice")
    db.save_chat_log("sess-1", "assistant", "回复", user_id="alice")
    db.upsert_chat_session("sess-1", "alice", "原提问")
    session._title_worker("sess-1", "alice", "原提问", "回复")  # 不该抛
    assert db.list_chat_sessions(user_id="alice")[0]["title"] == "原提问"


def test_worker_scopes_to_the_account(db, session):
    """会话属于别人时不该被改名。"""
    db.save_chat_log("sess-1", "user", "bob 的问题", user_id="bob")
    db.save_chat_log("sess-1", "assistant", "回复", user_id="bob")
    db.upsert_chat_session("sess-1", "bob", "bob 的问题")
    session._title_worker("sess-1", "alice", "bob 的问题", "回复")
    assert db.list_chat_sessions(user_id="bob")[0]["title"] == "bob 的问题"
