"""答复回话通道，以及只读的确认流水。

chat_answer 为什么是独立方法而不是复用 chat：被问的那一轮此刻**阻塞在等答复上**，
它自己的请求没法再收东西，答复必须由另一个 IPC worker 送进去。

approve_records 为什么只读:审批流程被拆掉之后剩下的是记录。它不返回「待办」，
桌面端也就长不出第二个入口 —— 用户明说了「千万不要说搞出一个审批流程」。
"""

from __future__ import annotations

import pytest

from cli.ipc import methods
from cli.ipc.methods import MethodError


def _result(method: str, **params):
    events = list(methods.dispatch(method, params))
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"{method} 没有返回 result 事件"
    return results[-1]


class FakeSession:
    """替身。真会话 start() 要几秒 MCP + pandas，这里只关心送达契约。"""

    def __init__(self, question_id="q1", accepts=True):
        self.pending_question_id = question_id
        self._accepts = accepts
        self.answers: list[tuple[str, str]] = []

    def answer_question(self, question_id: str, answer: str) -> bool:
        self.answers.append((question_id, answer))
        return self._accepts and question_id == self.pending_question_id


@pytest.fixture
def db(tmp_path, monkeypatch):
    from cli import approval_queue as aq

    path = tmp_path / "approvals.db"
    monkeypatch.setattr(aq, "DB_PATH", path)
    return path


class TestChatAnswer:
    def test_delivered_when_a_turn_is_waiting(self, monkeypatch):
        session = FakeSession("q1")
        monkeypatch.setattr("cli.ipc.session.find_waiting_session", lambda _: session)
        res = _result("chat_answer", question_id="q1", answer="allow")
        assert res["delivered"] is True
        assert session.answers == [("q1", "allow")]

    def test_not_delivered_when_no_turn_is_waiting(self, monkeypatch):
        """超时收尾之后点按钮会走到这里。

        必须说清没送达 —— 报 delivered=True 会让界面显示「已批准」，而那一轮其实
        早就按未作答结束了，操作根本没执行。
        """
        monkeypatch.setattr("cli.ipc.session.find_waiting_session", lambda _: None)
        res = _result("chat_answer", question_id="q1", answer="allow")
        assert res["delivered"] is False

    def test_not_delivered_when_the_session_refuses(self, monkeypatch):
        """已经答过一次了（同一张卡被点第二下）。"""
        monkeypatch.setattr("cli.ipc.session.find_waiting_session", lambda _: FakeSession("q1", accepts=False))
        assert _result("chat_answer", question_id="q1", answer="deny")["delivered"] is False

    def test_missing_question_id_is_a_param_error(self):
        """没有 id 就不知道该送给谁。静默成功会让人以为点上了。"""
        with pytest.raises(MethodError):
            list(methods.dispatch("chat_answer", {"answer": "allow"}))

    def test_blank_question_id_is_a_param_error(self):
        with pytest.raises(MethodError):
            list(methods.dispatch("chat_answer", {"question_id": "   ", "answer": "allow"}))

    def test_question_id_echoed_back(self, monkeypatch):
        """前端靠它把结果配到对应那张卡上 —— 一轮里可能有过好几张。"""
        monkeypatch.setattr("cli.ipc.session.find_waiting_session", lambda _: FakeSession("q7"))
        assert _result("chat_answer", question_id="q7", answer="allow")["question_id"] == "q7"

    def test_missing_answer_becomes_empty_not_a_crash(self, monkeypatch):
        """空答复交给会话层判断（自由文本的提问卡可以留空），这一层别自己发明默认值。"""
        session = FakeSession("q1")
        monkeypatch.setattr("cli.ipc.session.find_waiting_session", lambda _: session)
        _result("chat_answer", question_id="q1")
        assert session.answers == [("q1", "")]

    def test_free_text_answer_passes_through_verbatim(self, monkeypatch):
        session = FakeSession("q1")
        monkeypatch.setattr("cli.ipc.session.find_waiting_session", lambda _: session)
        _result("chat_answer", question_id="q1", answer="改成 1180 吧")
        assert session.answers == [("q1", "改成 1180 吧")]


class TestApproveRecords:
    def test_returns_decided_rows(self, db, monkeypatch):
        from cli import approval_queue as aq

        monkeypatch.setattr(methods, "_current_user_id", lambda: "alice")
        aq.log_decision(
            "set_stop_loss",
            {"code": "600519", "stop_loss": 1200},
            risk="confirm",
            source="desktop",
            decision="allow",
            user_id="alice",
            db_path=db,
        )
        res = _result("approve_records")
        assert res["count"] == 1
        assert res["items"][0]["status"] == aq.APPROVED
        assert res["items"][0]["decided_at"], "没有决定时间,界面上只能显示发起时间"

    def test_pending_items_are_excluded(self, db, monkeypatch):
        """待批项是定时任务留下的,还没有结论 —— 混进流水会又变成一份待办。"""
        from cli import approval_queue as aq

        monkeypatch.setattr(methods, "_current_user_id", lambda: "alice")
        aq.enqueue(
            "update_portfolio",
            {"code": "600519"},
            risk="confirm",
            source="schedule",
            user_id="alice",
            db_path=db,
        )
        assert _result("approve_records")["count"] == 0

    def test_scoped_to_the_current_account(self, db, monkeypatch):
        from cli import approval_queue as aq

        for who in ("alice", "bob"):
            aq.log_decision(
                "set_stop_loss",
                {"code": "600519"},
                risk="confirm",
                source="desktop",
                decision="allow",
                user_id=who,
                db_path=db,
            )
        monkeypatch.setattr(methods, "_current_user_id", lambda: "alice")
        assert _result("approve_records")["count"] == 1

    def test_empty_is_a_normal_result_not_an_error(self, db, monkeypatch):
        monkeypatch.setattr(methods, "_current_user_id", lambda: "alice")
        res = _result("approve_records")
        assert res["count"] == 0 and res["items"] == []
