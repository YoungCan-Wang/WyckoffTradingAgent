"""Explicit Shanghai observation windows, independent of the host timezone."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
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


def resume_effective_date(text: str, published_at: str) -> str:
    """Resolve explicit calendar dates only; never guess the next trading session."""
    explicit = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?[^，；。]{0,24}复牌", text)
    if explicit:
        try:
            return date(*(int(part) for part in explicit.groups())).isoformat()
        except ValueError:
            return ""
    relative = re.search(r"(明日|明天|后日|后天|次日)[^，；。]{0,20}复牌", text)
    published = event_time(published_at)
    if relative and published:
        days = 2 if relative.group(1) in {"后日", "后天"} else 1
        return (published.date() + timedelta(days=days)).isoformat()
    return ""
