from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from cli import approval_queue as aq


@pytest.fixture
def db(tmp_path):
    return tmp_path / "approvals.db"


def _enqueue(db, **kwargs):
    args = kwargs.pop("args", {"code": "002270", "stop_loss": 33.15})
    return aq.enqueue(
        kwargs.pop("tool_name", "update_portfolio"),
        args,
        risk=kwargs.pop("risk", "review"),
        source=kwargs.pop("source", "daemon"),
        user_id=kwargs.pop("user_id", "alice"),
        db_path=db,
        **kwargs,
    )


def _backdate(db, approval_id, hours):
    stamp = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE approvals SET created_at = ? WHERE id = ?", (stamp, approval_id))
    conn.commit()
    conn.close()


class TestEnqueue:
    def test_pending_after_enqueue(self, db):
        approval_id = _enqueue(db, schedule_id="mkt-open", summary="002270 止损 → 33.15")
        pending = aq.list_pending(db_path=db)
        assert [p.id for p in pending] == [approval_id]
        assert pending[0].schedule_id == "mkt-open"
        assert pending[0].status == aq.PENDING
        assert pending[0].user_id == "alice"

    def test_args_roundtrip(self, db):
        _enqueue(db, args={"code": "605007", "stop_loss": 13.0})
        assert aq.list_pending(db_path=db)[0].args == {"code": "605007", "stop_loss": 13.0}

    def test_owner_matches_requires_same_account(self, db):
        _enqueue(db, user_id="alice")
        record = aq.list_pending(db_path=db)[0]
        assert aq.owner_matches(record, "alice")
        assert not aq.owner_matches(record, "bob")
        assert not aq.owner_matches(record, "")

    def test_ordered_by_creation(self, db):
        first = _enqueue(db)
        second = _enqueue(db)
        _backdate(db, first, 1)
        assert [p.id for p in aq.list_pending(db_path=db)] == [first, second]


class TestDecide:
    def test_approve_returns_record(self, db):
        approval_id = _enqueue(db)
        record = aq.decide(approval_id, approved=True, db_path=db)
        assert record is not None
        assert record.status == aq.APPROVED
        assert aq.list_pending(db_path=db) == []

    def test_reject_clears_from_pending(self, db):
        approval_id = _enqueue(db)
        record = aq.decide(approval_id, approved=False, db_path=db)
        assert record is not None and record.status == aq.REJECTED
        assert aq.list_pending(db_path=db) == []

    def test_cannot_decide_twice(self, db):
        approval_id = _enqueue(db)
        aq.decide(approval_id, approved=True, db_path=db)
        assert aq.decide(approval_id, approved=False, db_path=db) is None

    def test_unknown_id_returns_none(self, db):
        assert aq.decide("nope", approved=True, db_path=db) is None


class TestExpiry:
    def test_expired_item_cannot_be_approved(self, db):
        """隔夜批准会按旧价成交，所以过期项必须硬拒。"""
        approval_id = _enqueue(db)
        _backdate(db, approval_id, aq.DEFAULT_TTL_HOURS + 1)
        assert aq.decide(approval_id, approved=True, db_path=db) is None
        assert aq.get(approval_id, db_path=db).status == aq.EXPIRED

    def test_expired_dropped_from_pending(self, db):
        fresh = _enqueue(db)
        stale = _enqueue(db)
        _backdate(db, stale, aq.DEFAULT_TTL_HOURS + 1)
        assert [p.id for p in aq.list_pending(db_path=db)] == [fresh]

    def test_within_ttl_still_approvable(self, db):
        approval_id = _enqueue(db)
        _backdate(db, approval_id, aq.DEFAULT_TTL_HOURS - 1)
        assert aq.decide(approval_id, approved=True, db_path=db) is not None

    def test_expire_stale_reports_count(self, db):
        for _ in range(3):
            _backdate(db, _enqueue(db), aq.DEFAULT_TTL_HOURS + 2)
        assert aq.expire_stale(db_path=db) == 3


class TestSummarize:
    def test_stop_loss_summary(self):
        """止损摘要必须挂在 set_stop_loss 上：update_portfolio 已不接受 stop_loss。"""
        args = {"code": "002270", "name": "法狮龙", "stop_loss": 33.15}
        assert aq.summarize("set_stop_loss", args) == "002270 法狮龙 止损 → 33.15"

    def test_batch_stop_loss_summary(self):
        args = {"items": [{"code": str(i), "stop_loss": 1.0} for i in range(189)]}
        assert aq.summarize("set_stop_loss", args) == "批量补止损 189 只"

    def test_portfolio_action_summary(self):
        args = {"code": "002270", "name": "法狮龙", "action": "add", "shares": 500}
        assert aq.summarize("update_portfolio", args) == "002270 法狮龙 add 500 股"

    def test_trade_summary(self):
        args = {"code": "600519", "name": "贵州茅台", "side": "buy", "shares": 100}
        assert aq.summarize("record_trade_fill", args) == "600519 贵州茅台 buy 100 股"

    def test_falls_back_to_tool_name(self):
        assert aq.summarize("write_file", {}) == "write_file"

    def test_sanitized_args_masks_nested_secrets(self):
        args = {"code": "1", "api_key": "secret", "items": [{"password": "p", "shares": 1}]}
        assert aq.sanitized_args(args) == {
            "code": "1",
            "api_key": "***",
            "items": [{"password": "***", "shares": 1}],
        }


class TestExecutionAudit:
    def test_success_is_recorded(self, db):
        approval_id = _enqueue(db)
        aq.decide(approval_id, approved=True, db_path=db)
        aq.record_execution(approval_id, {"updated": 1}, succeeded=True, db_path=db)

        record = aq.get(approval_id, db_path=db)
        assert record is not None and record.status == aq.EXECUTED
        assert record.executed_at
        assert '"updated": 1' in record.result_json

    def test_failure_is_recorded_without_retrying(self, db):
        approval_id = _enqueue(db)
        aq.decide(approval_id, approved=True, db_path=db)
        aq.record_execution(approval_id, {"error": "bad"}, succeeded=False, db_path=db)

        assert aq.get(approval_id, db_path=db).status == aq.FAILED
        assert aq.decide(approval_id, approved=True, db_path=db) is None
