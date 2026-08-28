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


class TestRiskReason:
    """档位理由随记录存下来，展示时不重算。"""

    def test_round_trips(self, db):
        approval_id = _enqueue(db, risk_reason="reason.over_nav", nav_ratio=0.062)
        record = aq.get(approval_id, db_path=db)
        assert record.risk_reason == "reason.over_nav"
        assert record.nav_ratio == pytest.approx(0.062)

    def test_survives_list_pending(self, db):
        _enqueue(db, risk_reason="reason.destructive_action")
        assert aq.list_pending(db_path=db)[0].risk_reason == "reason.destructive_action"

    def test_defaults_are_empty_not_null(self, db):
        """老调用方不传理由也要能工作，读出来是空串而不是 None。"""
        record = aq.get(_enqueue(db), db_path=db)
        assert record.risk_reason == ""
        assert record.nav_ratio == 0.0

    def test_migrates_existing_db_missing_the_columns(self, db):
        """已经在用的库要能加列，而不是启动就崩。"""
        _enqueue(db)
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE approvals DROP COLUMN risk_reason")
            conn.execute("ALTER TABLE approvals DROP COLUMN nav_ratio")

        fresh = _enqueue(db, risk_reason="reason.write_tool")
        assert aq.get(fresh, db_path=db).risk_reason == "reason.write_tool"
        assert len(aq.list_pending(db_path=db)) == 2


class TestDecisionRecords:
    """会话内的确认只留流水，不产生待办。

    这是审批流程被拆掉之后剩下的东西：确认在对话里当场问、当场执行，落库只为
    回答「谁在什么时候批了什么」。所以关键性质是它**进不了待批队列** —— 一旦
    进了，桌面端就会又长出一个待办入口,而那正是要删掉的东西。
    """

    def _log(self, db, decision, **kwargs):
        return aq.log_decision(
            kwargs.pop("tool_name", "update_portfolio"),
            kwargs.pop("args", {"code": "002270", "stop_loss": 33.15}),
            risk=kwargs.pop("risk", "confirm"),
            source=kwargs.pop("source", "desktop"),
            decision=decision,
            db_path=db,
            **kwargs,
        )

    def test_never_lands_in_pending(self, db):
        for decision in ("allow", "deny", ""):
            self._log(db, decision)
        assert aq.list_pending(db_path=db) == [], "确认记录跑进了待批队列 —— 待办入口会复活"

    def test_decision_maps_to_terminal_status(self, db):
        cases = {"allow": aq.APPROVED, "deny": aq.REJECTED, "": aq.EXPIRED}
        for decision, expected in cases.items():
            record_id = self._log(db, decision)
            assert aq.get(record_id, db_path=db).status == expected, decision

    def test_unknown_decision_is_expired_not_approved(self, db):
        """认不出来的答复绝不能算同意 —— 那是替用户做一个他没做过的决定。"""
        record_id = self._log(db, "maybe")
        assert aq.get(record_id, db_path=db).status == aq.EXPIRED

    def test_decided_at_is_set(self, db):
        """记录一写出来就是终态,没有「等待决策」的中间时刻。"""
        record = aq.get(self._log(db, "allow"), db_path=db)
        assert record.decided_at, "缺 decided_at,界面上就只能显示发起时间"

    def test_listed_newest_first(self, db):
        old = self._log(db, "allow", user_id="alice")
        new = self._log(db, "deny", user_id="alice")
        _backdate(db, old, 2)
        assert [r.id for r in aq.list_decisions(user_id="alice", db_path=db)] == [new, old]

    def test_scoped_to_account(self, db):
        self._log(db, "allow", user_id="alice")
        self._log(db, "allow", user_id="bob")
        assert [r.user_id for r in aq.list_decisions(user_id="alice", db_path=db)] == ["alice"]

    def test_excludes_pending_items(self, db):
        """待批项（定时任务留下的）不该混进确认记录 —— 它们还没有结论。"""
        _enqueue(db, user_id="alice")
        logged = self._log(db, "allow", user_id="alice")
        assert [r.id for r in aq.list_decisions(user_id="alice", db_path=db)] == [logged]

    def test_args_are_sanitized(self, db):
        record_id = self._log(db, "allow", args={"code": "605007", "stop_loss": 13.0})
        assert aq.get(record_id, db_path=db).args == {"code": "605007", "stop_loss": 13.0}

    def test_risk_reason_round_trips(self, db):
        record = aq.get(
            self._log(db, "allow", risk_reason="reason.over_nav", nav_ratio=0.062),
            db_path=db,
        )
        assert record.risk_reason == "reason.over_nav"
        assert record.nav_ratio == pytest.approx(0.062)

    def test_limit_is_clamped(self, db):
        """limit 直接拼进 SQL,越界值不能变成负数 LIMIT 或者拉全表。"""
        self._log(db, "allow", user_id="alice")
        assert len(aq.list_decisions(user_id="alice", limit=0, db_path=db)) == 1
        assert len(aq.list_decisions(user_id="alice", limit=-5, db_path=db)) == 1


def test_concurrent_decisions_execute_once(tmp_path):
    """手机和电脑同时批同一项，只能有一个成功。

    多设备遥控之后这不再是理论场景：两块屏幕都显示着同一个待批项，用户在手机上
    点了、又想起电脑上也开着。重复执行一次卖出是真金白银。

    `decide()` 靠 `BEGIN IMMEDIATE` + `UPDATE ... WHERE status='pending'` 的
    rowcount 守住。这条测试之前不存在 —— 只有串行的 test_cannot_decide_twice。
    """
    import threading

    db = tmp_path / "race.db"
    approval_id = aq.enqueue(
        "set_stop_loss",
        {"code": "600519", "stop_loss": 1200},
        risk="confirm",
        source="test",
        summary="设止损",
        user_id="alice",
        db_path=db,
    )

    won: list[object] = []
    errors: list[str] = []

    def attempt() -> None:
        try:
            if aq.decide(approval_id, approved=True, db_path=db) is not None:
                won.append(1)
        except Exception as exc:  # noqa: BLE001 - 锁竞争不该抛，抛了就是缺陷
            errors.append(repr(exc))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"并发决策抛异常：{errors}"
    assert len(won) == 1, f"应恰好一个成功，实际 {len(won)}"
    assert aq.get(approval_id, db_path=db).status == aq.APPROVED
