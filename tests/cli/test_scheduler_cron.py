from __future__ import annotations

from datetime import datetime

from cli.scheduler import cron_matches_now


def test_cron_matches_now_uses_provided_time_instead_of_wall_clock():
    at = datetime(2026, 1, 5, 9, 25)  # Monday

    assert cron_matches_now("25 9 * * 1-5", at=at)
    assert not cron_matches_now("26 9 * * 1-5", at=at)


def test_cron_matches_now_checks_weekday_field():
    saturday = datetime(2026, 1, 3, 9, 25)

    assert not cron_matches_now("25 9 * * 1-5", at=saturday)
