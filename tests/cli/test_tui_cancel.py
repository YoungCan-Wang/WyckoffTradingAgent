from __future__ import annotations

from datetime import datetime, timedelta

from cli.tui import _pending_schedule_check_minutes, _should_force_exit_busy_cancel


def test_busy_cancel_requires_existing_cancel_signal():
    assert not _should_force_exit_busy_cancel(False, 10.0, 10.2)


def test_busy_cancel_second_press_forces_exit_inside_window():
    assert _should_force_exit_busy_cancel(True, 10.0, 11.0)


def test_busy_cancel_second_press_outside_window_retries_cancel():
    assert not _should_force_exit_busy_cancel(True, 10.0, 12.0)


def test_pending_schedule_check_minutes_returns_only_now_on_first_check():
    now = datetime(2026, 1, 5, 9, 30)

    assert _pending_schedule_check_minutes(None, now) == [now]


def test_pending_schedule_check_minutes_backfills_skipped_minutes():
    last = datetime(2026, 1, 5, 9, 30)
    now = last + timedelta(minutes=3)

    minutes = _pending_schedule_check_minutes(last, now)

    assert minutes == [
        datetime(2026, 1, 5, 9, 31),
        datetime(2026, 1, 5, 9, 32),
        datetime(2026, 1, 5, 9, 33),
    ]


def test_pending_schedule_check_minutes_caps_long_gaps():
    last = datetime(2026, 1, 5, 9, 0)
    now = last + timedelta(hours=2)

    minutes = _pending_schedule_check_minutes(last, now)

    assert len(minutes) == 15
    assert minutes[0] == now - timedelta(minutes=14)
    assert minutes[-1] == now
