"""轻量 cron 调度器 — TUI 定时触发 Agent 任务。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEDULES_PATH = Path.home() / ".wyckoff" / "schedules.json"

SCHEDULE_CHECK_MAX_CATCHUP_MINUTES = 15

DEFAULT_PRESETS: list[dict] = [
    {
        "id": "mkt-open",
        "name": "盘前风控检查",
        "cron": "25 9 * * 1-5",
        "action": "/checkup",
        "notify": True,
        "enabled": False,
    },
    {
        "id": "eod-review",
        "name": "收盘复盘",
        "cron": "5 15 * * 1-5",
        "action": "大盘水温怎么样？持仓做个体检，给我今天的总结和明天的策略建议",
        "notify": True,
        "enabled": False,
    },
]


@dataclass
class Schedule:
    id: str
    name: str
    cron: str
    action: str
    notify: bool = True
    enabled: bool = True
    last_fired: str = ""
    last_status: str = "never"
    last_error: str = ""


def load_schedules() -> list[Schedule]:
    if not SCHEDULES_PATH.exists():
        schedules = [Schedule(**p) for p in DEFAULT_PRESETS]
        save_schedules(schedules)
        return schedules
    try:
        raw = json.loads(SCHEDULES_PATH.read_text(encoding="utf-8"))
        return [Schedule(**s) for s in raw]
    except Exception:
        logger.warning("Failed to load schedules, using defaults")
        return [Schedule(**p) for p in DEFAULT_PRESETS]


def save_schedules(schedules: list[Schedule]) -> None:
    SCHEDULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULES_PATH.write_text(
        json.dumps([asdict(s) for s in schedules], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pending_check_minutes(last_check_at: datetime | None, now: datetime) -> list[datetime]:
    """Minutes to evaluate since the last check, so a delayed tick (e.g. blocked by a
    long-running task) doesn't silently skip a cron minute that fell in the gap."""
    if last_check_at is None or last_check_at >= now:
        return [now]
    gap_minutes = min(int((now - last_check_at).total_seconds() // 60), SCHEDULE_CHECK_MAX_CATCHUP_MINUTES)
    return [now - timedelta(minutes=offset) for offset in range(gap_minutes - 1, -1, -1)]


def due_schedules(
    schedules: list[Schedule],
    *,
    last_check_at: datetime | None,
    now: datetime,
) -> list[tuple[Schedule, str]]:
    """返回本轮应触发的 (schedule, minute_key)，并跳过该分钟已触发过的。"""
    due: list[tuple[Schedule, str]] = []
    for minute in pending_check_minutes(last_check_at, now):
        minute_key = minute.strftime("%Y-%m-%dT%H:%M")
        for schedule in schedules:
            if not schedule.enabled or schedule.last_fired.startswith(minute_key):
                continue
            if cron_matches_now(schedule.cron, at=minute):
                due.append((schedule, minute_key))
    return due


def cron_matches_now(cron: str, at: datetime | None = None) -> bool:
    now = at or datetime.now()
    fields = cron.strip().split()
    if len(fields) != 5:
        return False
    checks = [
        (fields[0], now.minute, 0, 59),
        (fields[1], now.hour, 0, 23),
        (fields[2], now.day, 1, 31),
        (fields[3], now.month, 1, 12),
        (fields[4], now.isoweekday() % 7, 0, 6),
    ]
    return all(_field_matches(pat, val, lo, hi) for pat, val, lo, hi in checks)


def next_scheduled_time(schedule: Schedule, *, at: datetime | None = None, search_days: int = 8) -> datetime | None:
    """Find the next matching minute without requiring a background scheduler."""
    if not schedule.enabled:
        return None
    current = (at or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = current + timedelta(days=max(search_days, 1))
    while current <= limit:
        if cron_matches_now(schedule.cron, at=current):
            return current
        current += timedelta(minutes=1)
    return None


def schedule_status(schedules: list[Schedule], *, at: datetime | None = None) -> list[dict[str, str | bool]]:
    """Return reader-facing schedule state for TUI and future API consumers."""
    return [
        {
            "id": schedule.id,
            "name": schedule.name,
            "enabled": schedule.enabled,
            "cron": schedule.cron,
            "last_fired": schedule.last_fired,
            "last_status": schedule.last_status,
            "last_error": schedule.last_error,
            "next_run": next_time.isoformat(timespec="minutes")
            if (next_time := next_scheduled_time(schedule, at=at))
            else "",
        }
        for schedule in schedules
    ]


def _field_matches(pattern: str, value: int, lo: int, hi: int) -> bool:
    if pattern == "*":
        return True
    for part in pattern.split(","):
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            start = lo if base == "*" else int(base)
            if step > 0 and value >= start and (value - start) % step == 0:
                return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
        elif value == int(part):
            return True
    return False
