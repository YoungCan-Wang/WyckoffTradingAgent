"""就地确认：写操作停在当轮等答复，不再挪去「审批」页。

原来的路径是入队 + 返回 queued，用户去审批页批准，由 approve_decide 在另一个请求里
执行。两个后果：确认脱离了发起它的对话（批完回来找不到上下文），而且那一轮往往
没有正文，历史落盘被 `if reply` 跳过 —— 用户的原话是「会话历史没有保存，我找不到
会话了」。

现在按它本来的性质处理：卡片进当轮事件流，工具线程阻塞等答复，同意就在同一轮里执行。
这里测的是那几条容易悄悄反向的性质：超时不能读成拒绝、答复要送得进被阻塞的那一轮、
以及卡片必须真的发得出去（否则双方互等到超时）。
"""

from __future__ import annotations

import threading

import pytest

from cli.ipc.session import ASK_HEARTBEAT_SECONDS, ASK_TIMEOUT_SECONDS, DesktopSession


@pytest.fixture
def db(tmp_path, monkeypatch):
    """确认记录写进临时库，别碰用户真实的 ~/.wyckoff/approvals.db。"""
    from cli import approval_queue as aq

    path = tmp_path / "approvals.db"
    monkeypatch.setattr(aq, "DB_PATH", path)
    return path


def _answer_from_another_thread(session, value, *, delay=0.05):
    """模拟前端：另一个线程送答复进来。

    这就是真实拓扑 —— 被问的那一轮阻塞在 wait 上，答复作为另一个 IPC 请求落在
    stdio 线程池的别的 worker 上。
    """

    def send():
        for _ in range(200):
            question_id = session.pending_question_id
            if question_id:
                session.answer_question(question_id, value)
                return
            threading.Event().wait(delay)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    return thread


class TestTimeoutBudget:
    """等待上限必须留在 bridge 的静默看门狗以内。"""

    def test_under_the_bridge_watchdog(self):
        """desktop/src/python-bridge.js 的 IDLE_TIMEOUT_MS 是 180s。

        等过了它，bridge 会替这一轮补发 error+end，前端那一轮就收尾了 —— 之后
        我们发的答复事件没人接，用户点了按钮却什么也不会发生。
        """
        assert ASK_TIMEOUT_SECONDS < 180.0

    def test_heartbeat_fits_several_times_over(self):
        """心跳要密到看门狗不会在人思考的时候到点。"""
        assert ASK_HEARTBEAT_SECONDS < 180.0 / 2
        assert ASK_TIMEOUT_SECONDS / ASK_HEARTBEAT_SECONDS >= 3


class TestConfirmGate:
    def test_allow_runs_in_this_turn(self, db):
        session = DesktopSession("s1")
        _answer_from_another_thread(session, "allow")
        assert session._confirm("set_stop_loss", {"code": "600519", "stop_loss": 1200})["action"] == "allow"

    def test_deny_stops_the_operation(self, db):
        session = DesktopSession("s2")
        _answer_from_another_thread(session, "deny")
        assert session._confirm("set_stop_loss", {"code": "600519", "stop_loss": 1200})["action"] == "deny"

    def test_no_answer_is_timeout_not_deny(self, db, monkeypatch):
        """人不在,绝不能替他做决定。

        说成「用户拒绝」是伪造一件没发生的事:模型只能照着那个措辞往下写,
        用户会在回复里读到自己从没做过的选择。cli/tools.py 也据此分两套措辞。
        """
        import cli.ipc.session as mod

        monkeypatch.setattr(mod, "ASK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(mod, "ASK_HEARTBEAT_SECONDS", 0.02)
        session = DesktopSession("s3")
        assert session._confirm("update_portfolio", {"code": "600519"})["action"] == "timeout"

    def test_never_returns_queued(self, db):
        """queued 是旧路径的返回值 —— 它会让模型说「已提交审批」。"""
        session = DesktopSession("s4")
        _answer_from_another_thread(session, "allow")
        assert session._confirm("set_stop_loss", {"code": "600519"})["action"] != "queued"

    def test_card_reaches_the_event_stream(self, db):
        """卡片必须真的进 outbox。

        发不出去的话前端永远收不到要它作答的那张卡,双方互等到超时 —— 这正是把
        runtime 挪到独立线程（_stream_with_outbox）要解决的问题。
        """
        session = DesktopSession("s5")
        _answer_from_another_thread(session, "allow")
        session._confirm("set_stop_loss", {"code": "600519", "stop_loss": 1200})
        cards = [e for e in list(session._outbox.queue) if e.get("type") == "confirm_request"]
        assert len(cards) == 1
        card = cards[0]
        assert card["question_id"], "没有 question_id 就答不回去"
        assert card["tool_name"] == "set_stop_loss"
        assert card["args"] == {"code": "600519", "stop_loss": 1200}
        assert card["risk"], "缺档位,卡片上就没有「这有多重」这一层信息"


class TestAnswerDelivery:
    def test_stale_question_id_is_refused(self, db):
        """过期或伪造的 id 不能算送达 —— 否则界面会显示一个假的「已同意」。"""
        session = DesktopSession("s6")
        assert session.answer_question("nope", "allow") is False

    def test_second_click_does_not_land(self, db):
        session = DesktopSession("s7")
        answered: list[bool] = []
        ready = threading.Event()

        def click_twice():
            for _ in range(200):
                question_id = session.pending_question_id
                if question_id:
                    answered.append(session.answer_question(question_id, "allow"))
                    answered.append(session.answer_question(question_id, "deny"))
                    ready.set()
                    return
                threading.Event().wait(0.05)

        threading.Thread(target=click_twice, daemon=True).start()
        session._confirm("set_stop_loss", {"code": "600519"})
        ready.wait(2.0)
        assert answered == [True, False], "第二次点击也算送达的话,后一个答复会覆盖前一个决定"

    def test_no_pending_question_when_idle(self, db):
        assert DesktopSession("s8").pending_question_id == ""

    def test_find_waiting_session_does_not_create_one(self):
        """送答复只该找已经在等的那一轮。

        走 get_session 会在找不到时**新建**一个（还跑 start(),六秒起）——
        前端拿着过期卡片点一下就会凭空造出一个会话。
        """
        from cli.ipc.session import find_waiting_session

        assert find_waiting_session("") is None
        assert find_waiting_session("does-not-exist") is None


class TestAskUserQuestion:
    def test_answer_passes_through(self, db):
        session = DesktopSession("s9")
        _answer_from_another_thread(session, "200 股")
        assert session._ask("要买多少?", ["100 股", "200 股"]) == "200 股"

    def test_question_card_carries_the_options(self, db):
        session = DesktopSession("s10")
        _answer_from_another_thread(session, "100 股")
        session._ask("要买多少?", ["100 股", "200 股"], True, "100 股")
        card = next(e for e in list(session._outbox.queue) if e.get("type") == "question_request")
        assert card["options"] == ["100 股", "200 股"]
        assert card["allow_free_text"] is True
        assert card["default_answer"] == "100 股"

    def test_timeout_returns_the_sentinel_not_prose(self, db, monkeypatch):
        """超时要回哨兵,不能回一句自然语言。

        回「已超时未作答」这类句子,模型会把它当成用户的答复内容继续推理
        （cli/tools.py 靠 == ASK_USER_TIMEOUT_SENTINEL 分流）。
        """
        import cli.ipc.session as mod
        from cli.tools import ASK_USER_TIMEOUT_SENTINEL

        monkeypatch.setattr(mod, "ASK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(mod, "ASK_HEARTBEAT_SECONDS", 0.02)
        assert DesktopSession("s11")._ask("在吗?") == ASK_USER_TIMEOUT_SENTINEL

    def test_default_answer_wins_over_the_sentinel(self, db, monkeypatch):
        """给了默认值就用默认值 —— 模型自己声明过没人答时该走哪条。"""
        import cli.ipc.session as mod

        monkeypatch.setattr(mod, "ASK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(mod, "ASK_HEARTBEAT_SECONDS", 0.02)
        assert DesktopSession("s12")._ask("在吗?", None, True, "跳过") == "跳过"

    def test_second_question_is_refused_not_overwritten(self, db, monkeypatch):
        """一轮里同时问两件事时,后来者超时,而不是顶掉前一个。

        顶掉的话前一个会永久挂在 wait 上 —— 那一轮再也不会结束。
        """
        import cli.ipc.session as mod

        monkeypatch.setattr(mod, "ASK_TIMEOUT_SECONDS", 0.4)
        monkeypatch.setattr(mod, "ASK_HEARTBEAT_SECONDS", 0.1)
        session = DesktopSession("s13")
        first_id: list[str] = []
        held = threading.Event()

        def ask_first():
            first_id.append("in")
            session._ask("第一个问题")
            held.set()

        thread = threading.Thread(target=ask_first, daemon=True)
        thread.start()
        for _ in range(100):
            if session.pending_question_id:
                break
            threading.Event().wait(0.01)
        pending_before = session.pending_question_id
        assert pending_before, "第一个问题没挂上"

        from cli.tools import ASK_USER_TIMEOUT_SENTINEL

        assert session._ask("第二个问题") == ASK_USER_TIMEOUT_SENTINEL
        assert session.pending_question_id == pending_before, "第一个问题被顶掉了,那一轮会永久挂住"
        held.wait(2.0)
        thread.join(timeout=2.0)


class TestDecisionRecord:
    """记录是记录,不是待办。"""

    def test_allow_is_recorded(self, db):
        from cli import approval_queue as aq

        session = DesktopSession("s14")
        _answer_from_another_thread(session, "allow")
        session._confirm("set_stop_loss", {"code": "600519", "stop_loss": 1200})
        records = aq.list_decisions(user_id="", db_path=db)
        assert [r.status for r in records] == [aq.APPROVED]
        assert records[0].source == "desktop"
        assert records[0].schedule_id == "s14", "没记会话 id,就答不出「这是在哪次对话里批的」"

    def test_deny_is_recorded_too(self, db):
        from cli import approval_queue as aq

        session = DesktopSession("s15")
        _answer_from_another_thread(session, "deny")
        session._confirm("set_stop_loss", {"code": "600519"})
        assert [r.status for r in aq.list_decisions(user_id="", db_path=db)] == [aq.REJECTED]

    def test_record_never_becomes_a_todo(self, db):
        """写进去的是终态。留成 pending 的话待批列表会重新长出来。"""
        from cli import approval_queue as aq

        session = DesktopSession("s16")
        _answer_from_another_thread(session, "allow")
        session._confirm("set_stop_loss", {"code": "600519"})
        assert aq.list_pending(db_path=db) == []

    def test_record_failure_does_not_block_the_operation(self, db, monkeypatch):
        """留痕是附加价值。库锁了不该让用户批不动。"""
        from cli import approval_queue as aq

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(aq, "log_decision", boom)
        session = DesktopSession("s17")
        _answer_from_another_thread(session, "allow")
        assert session._confirm("set_stop_loss", {"code": "600519"})["action"] == "allow"
