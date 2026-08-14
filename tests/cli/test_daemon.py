from __future__ import annotations

import multiprocessing
from datetime import datetime, timedelta

import pytest

from cli import daemon
from cli.scheduler import Schedule, due_schedules, pending_check_minutes


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "daemon.lock"


def _hold_lock(lock_path, ready, release):
    from pathlib import Path

    from cli import daemon as child_daemon

    with child_daemon.single_instance_lock(Path(lock_path)):
        ready.set()
        release.wait(timeout=10)


class TestSingleInstance:
    def test_lock_acquired_and_released(self, lock):
        with daemon.single_instance_lock(lock):
            assert lock.exists()
        with daemon.single_instance_lock(lock):
            pass

    def test_pid_written(self, lock):
        with daemon.single_instance_lock(lock):
            assert daemon.read_lock_pid(lock) is not None

    def test_second_instance_refused(self, lock):
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(str(lock), ready, release))
        proc.start()
        try:
            assert ready.wait(timeout=10)
            with pytest.raises(daemon.DaemonLockBusy):
                with daemon.single_instance_lock(lock):
                    pass
            assert daemon.is_daemon_running(lock) is True
        finally:
            release.set()
            proc.join(timeout=10)

    def test_not_running_when_lock_absent(self, lock):
        assert daemon.is_daemon_running(lock) is False

    def test_not_running_when_lock_stale(self, lock):
        lock.write_text("99999", encoding="utf-8")
        assert daemon.is_daemon_running(lock) is False

    def test_main_loop_has_distinct_lock_busy_exit_code(self, lock):
        with daemon.single_instance_lock(lock):
            assert daemon.main_loop(lock_path=lock, poll_seconds=0) == daemon.LOCK_BUSY_EXIT_CODE


class TestDueSchedules:
    def _sched(self, **kwargs):
        base = {"id": "s1", "name": "test", "cron": "* * * * *", "action": "ping"}
        base.update(kwargs)
        return Schedule(**base)

    def test_matching_minute_is_due(self):
        now = datetime(2026, 8, 11, 9, 25)
        due = due_schedules([self._sched()], last_check_at=None, now=now)
        assert [s.id for s, _ in due] == ["s1"]

    def test_disabled_never_due(self):
        now = datetime(2026, 8, 11, 9, 25)
        assert due_schedules([self._sched(enabled=False)], last_check_at=None, now=now) == []

    def test_already_fired_minute_skipped(self):
        now = datetime(2026, 8, 11, 9, 25)
        sched = self._sched(last_fired="2026-08-11T09:25")
        assert due_schedules([sched], last_check_at=None, now=now) == []

    def test_gap_is_backfilled(self):
        """定时器被长任务拖延后，跳过的 cron 分钟必须补上。"""
        now = datetime(2026, 8, 11, 9, 30)
        last = now - timedelta(minutes=3)
        due = due_schedules([self._sched(cron="28 9 * * *")], last_check_at=last, now=now)
        assert [key for _, key in due] == ["2026-08-11T09:28"]

    def test_last_checked_minute_not_refired(self):
        """上次检查那一分钟已评估过，不能再补一次，否则同一任务触发两回。"""
        now = datetime(2026, 8, 11, 9, 30)
        last = now - timedelta(minutes=3)
        due = due_schedules([self._sched(cron="27 9 * * *")], last_check_at=last, now=now)
        assert due == []

    def test_catchup_is_capped(self):
        now = datetime(2026, 8, 11, 9, 30)
        minutes = pending_check_minutes(now - timedelta(hours=6), now)
        assert len(minutes) == 15

    def test_first_check_only_evaluates_now(self):
        now = datetime(2026, 8, 11, 9, 30)
        assert pending_check_minutes(None, now) == [now]


class TestRunDueSchedules:
    def test_records_failure_on_schedule(self, monkeypatch, tmp_path):
        saved: list = []
        sched = Schedule(id="s1", name="t", cron="* * * * *", action="ping")

        monkeypatch.setattr(daemon, "LOCK_PATH", tmp_path / "d.lock")
        monkeypatch.setattr("cli.scheduler.load_schedules", lambda: [sched])
        monkeypatch.setattr("cli.scheduler.save_schedules", lambda s: saved.append(s))
        monkeypatch.setattr("cli.approval_queue.expire_stale", lambda **_k: 0)
        monkeypatch.setattr(
            "cli.headless.run_once",
            lambda *_a, **_k: __import__("cli.headless", fromlist=["HeadlessResult"]).HeadlessResult(
                ok=False, error="llm_failed"
            ),
        )

        daemon.run_due_schedules(None, datetime(2026, 8, 11, 9, 25))
        assert sched.last_status == "failed"
        assert sched.last_error == "llm_failed"
        assert sched.last_fired == "2026-08-11T09:25"
        assert saved

    def test_no_save_when_nothing_due(self, monkeypatch, tmp_path):
        saved: list = []
        sched = Schedule(id="s1", name="t", cron="0 3 * * *", action="ping")

        monkeypatch.setattr(daemon, "LOCK_PATH", tmp_path / "d.lock")
        monkeypatch.setattr("cli.scheduler.load_schedules", lambda: [sched])
        monkeypatch.setattr("cli.scheduler.save_schedules", lambda s: saved.append(s))
        monkeypatch.setattr("cli.approval_queue.expire_stale", lambda **_k: 0)

        daemon.run_due_schedules(None, datetime(2026, 8, 11, 9, 25))
        assert saved == []
