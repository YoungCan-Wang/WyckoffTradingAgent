from __future__ import annotations

import pytest

from cli import approval_queue as aq
from cli.headless import DaemonGuard, _consume
from cli.tools import ASK_USER_TIMEOUT_SENTINEL, ToolRegistry


@pytest.fixture
def db(tmp_path):
    return tmp_path / "approvals.db"


class TestGuardTiers:
    def test_set_stop_loss_allowed(self, db):
        guard = DaemonGuard(source="daemon", db_path=db)
        result = guard.confirm("set_stop_loss", {"code": "002270", "stop_loss": 33.15})
        assert result["action"] == "allow"
        assert guard.queued == []
        assert aq.list_pending(db_path=db) == []

    def test_update_portfolio_never_auto_even_with_stop_loss(self, db):
        """update_portfolio 不接受 stop_loss；带上这个字段也不该被当成补止损放行。"""
        guard = DaemonGuard(source="daemon", db_path=db)
        result = guard.confirm("update_portfolio", {"code": "002270", "stop_loss": 33.15})
        assert result["action"] == "queued"

    def test_share_change_is_queued(self, db):
        guard = DaemonGuard(source="daemon", schedule_id="eod", db_path=db)
        result = guard.confirm("update_portfolio", {"code": "002270", "shares": 500, "cost_price": 30.0})
        assert result["action"] == "queued"
        pending = aq.list_pending(db_path=db)
        assert len(pending) == 1
        assert pending[0].schedule_id == "eod"
        assert guard.queued == [pending[0].id]

    def test_queued_message_is_not_a_refusal(self, db):
        """措辞必须排除「用户拒绝」，否则模型会把没发生的决定写进回复。"""
        guard = DaemonGuard(source="daemon", db_path=db)
        message = guard.confirm("record_trade_fill", {"code": "x", "shares": 1})["message"]
        assert "拒绝" in message and "这不是拒绝" in message
        assert "待批准" in message

    def test_sell_is_queued(self, db):
        guard = DaemonGuard(source="daemon", db_path=db)
        args = {"code": "605007", "action": "sell", "shares": 100}
        assert guard.confirm("update_portfolio", args)["action"] == "queued"


class TestGuardWiring:
    def test_queued_action_surfaces_custom_message(self, db):
        """ToolRegistry 必须透传入队文案，不能落回超时措辞。"""
        guard = DaemonGuard(source="daemon", db_path=db)
        registry = ToolRegistry()
        registry.set_confirm_callback(guard.confirm)
        blocked = registry.execute("update_portfolio", {"code": "x", "shares": 100, "cost_price": 10.0})
        assert "待批准队列" in blocked["error"]
        assert "超时" not in blocked["error"]

    def test_high_risk_blocked_without_callback(self):
        """无回调时高风险工具必须被拦截，不能默认放行。"""
        registry = ToolRegistry()
        blocked = registry.execute("update_portfolio", {"code": "x", "shares": 100})
        assert "error" in blocked and "拦截" in blocked["error"]

    def test_ask_never_reads_stdin(self, db, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("daemon must never read stdin")

        monkeypatch.setattr("builtins.input", _boom)
        guard = DaemonGuard(source="daemon", db_path=db)
        assert guard.ask("要买多少股？", [], True, "") == ASK_USER_TIMEOUT_SENTINEL


class TestConsume:
    def _guard(self, db):
        return DaemonGuard(source="daemon", db_path=db)

    def test_done_event(self, db):
        events = [{"type": "text-delta"}, {"type": "done", "text": "完成", "rounds": 3}]
        result = _consume(iter(events), self._guard(db))
        assert result.ok and result.text == "完成" and result.rounds == 3

    def test_turn_failed(self, db):
        result = _consume(iter([{"type": "turn_failed", "error": "llm_failed"}]), self._guard(db))
        assert not result.ok and result.error == "llm_failed"

    def test_cancelled(self, db):
        result = _consume(iter([{"type": "turn_cancelled"}]), self._guard(db))
        assert not result.ok and result.error == "cancelled"

    def test_missing_terminal_event_is_failure(self, db):
        result = _consume(iter([{"type": "text-delta"}]), self._guard(db))
        assert not result.ok and "terminal" in result.error

    def test_queued_ids_reported(self, db):
        guard = self._guard(db)
        guard.confirm("update_portfolio", {"code": "x", "shares": 1, "cost_price": 2.0})
        result = _consume(iter([{"type": "done", "text": "ok"}]), guard)
        assert len(result.queued) == 1
