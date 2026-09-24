"""Explicit Shanghai observation windows, independent of the host timezone."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def event_time(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A date-only publication has no known intraday time. Conservatively place
    # it at day end so an 08:15 replay cannot see later announcements.
    if len(text) == 10:
        value = datetime.combine(value.date(), time(23, 59, 59, 999000))
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def observation_cutoff(raw: str) -> datetime:
    value = event_time(raw)
    if value is None:
        raise ValueError("as_of must be an ISO date or datetime; naive values use Asia/Shanghai")
    return value
