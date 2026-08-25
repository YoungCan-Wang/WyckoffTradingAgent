"""轻量 cron 调度器 — TUI 定时触发 Agent 任务。"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

from cli import platform_lock

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


_SCHEDULE_FIELDS = frozenset(f.name for f in fields(Schedule))


def load_schedules() -> list[Schedule]:
    """读全部任务。文件不存在时返回空列表。

    以前这里会把 DEFAULT_PRESETS 写进文件再返回，于是「一个任务都没有」不是一个
    稳定状态 —— 用户删空之后，下次读又长出两个。预置现在只作为界面上的推荐项
    （用户点「添加」才真正落盘），不再自动注入。
    """
    if not SCHEDULES_PATH.exists():
        return []
    try:
        raw = json.loads(SCHEDULES_PATH.read_text(encoding="utf-8"))
    except Exception:
        # 文件坏了不能当成「没有任务」—— 那样界面显示空的，用户新建一个就把原来的
        # 覆盖掉了。抛出去让调用方显示错误。
        logger.warning("schedules.json 解析失败", exc_info=True)
        raise
    out: list[Schedule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # 只取 dataclass 认识的字段：Schedule(**item) 遇到多余的键会抛 TypeError，
        # 而以前那个宽 except 会把整份用户数据换成预置。宁可丢掉不认识的字段。
        known = {k: v for k, v in item.items() if k in _SCHEDULE_FIELDS}
        try:
            out.append(Schedule(**known))
        except TypeError:
            logger.warning("跳过一条格式不对的任务: %r", item)
    return out


def save_schedules(schedules: list[Schedule]) -> None:
    """整份写回。原子写 —— 进程被杀不会留下截断的 JSON。"""
    SCHEDULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([asdict(s) for s in schedules], ensure_ascii=False, indent=2)
    tmp = SCHEDULES_PATH.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, SCHEDULES_PATH)


LOCK_PATH = SCHEDULES_PATH.with_suffix(".lock")


@contextmanager
def schedules_lock(timeout: float = 5.0) -> Iterator[None]:
    """把「读全部 → 改一条 → 写回」串起来。

    ## 为什么要跨进程锁

    `save_schedules` 是整文件覆写，而每次修改都是读-改-写三步。两个写入者各自基于
    自己读到的旧快照写回，后写的会把先写的整个抹掉。

    这里比 `chart_annotations` 的模块级 threading.Lock 更麻烦：**daemon 是独立
    进程**（`desktop/src/main.js` spawn 出来的），它每 tick 都会写回 last_fired /
    last_status。同进程的锁管不到它，所以要文件锁。

    `platform_lock.try_acquire` 是非阻塞的，所以这里自己轮询等待 —— 拿不到就抛，
    让调用方报错而不是静默丢掉用户的修改。
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open(platform_lock.lock_mode())
    deadline = time.monotonic() + timeout
    try:
        while not platform_lock.try_acquire(handle):
            if time.monotonic() >= deadline:
                raise platform_lock.LockBusy(f"schedules.json 被占用超过 {timeout}s")
            time.sleep(0.05)
        try:
            yield
        finally:
            platform_lock.release(handle)
    finally:
        handle.close()


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


def next_scheduled_time(
    schedule: Schedule,
    *,
    at: datetime | None = None,
    # 66 天而不是 8 天。
    #
    # 「每月几号」的任务在 8 天窗口里几乎必然搜不到 —— 实测「每月 5 号」「每月 20
    # 号」都返回 None，界面上就是「下次运行 —」，看起来像坏了。
    #
    # 66 = 两个最长月（31+31）再留几天余量：29-31 号的任务遇上 2 月会跳过那个月
    # （标准 cron 语义），所以最坏情况要跨过两个月才能命中。
    search_days: int = 66,
) -> datetime | None:
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
            # 编辑表单要回填它。不返回的话点「编辑」看到的是空的内容框，
            # 保存就把原来那句话清掉了。
            "action": schedule.action,
            "last_fired": schedule.last_fired,
            "last_status": schedule.last_status,
            "last_error": schedule.last_error,
            "next_run": next_time.isoformat(timespec="minutes")
            if (next_time := next_scheduled_time(schedule, at=at))
            else "",
        }
        for schedule in schedules
    ]


_FIELD_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_FIELD_NAMES = ["分钟", "小时", "日", "月", "星期"]


def validate_cron(cron: str) -> str | None:
    """校验 cron 字符串。合法返回 None，否则返回中文的错误原因。

    ## 为什么这是必须的

    `_field_matches` 里的 `int()` 遇到非数字会抛 `ValueError`，一路穿出
    `cron_matches_now`，最后被 `cli/daemon.py` 的宽 except 抓住 —— 那一轮
    **所有**任务都被跳过，不只是坏的那一个。一个坏值能让整个调度器静默停摆。

    另外 `"1-5/2"` 这种形状会先命中 `/` 分支，然后 `int("1-5")` 崩掉。标准 cron
    支持它，这里不支持，所以要显式拒绝而不是让它在运行期炸。

    字段数不对（比如 6 字段）不会崩，但 `cron_matches_now` 直接返回 False ——
    任务永远不触发，而界面上看不出任何异常。也要拦。
    """
    text = str(cron or "").strip()
    if not text:
        return "不能为空"
    fields = text.split()
    if len(fields) != 5:
        return f"需要 5 个字段（分 时 日 月 周），收到 {len(fields)} 个"

    for pattern, name, (lo, hi) in zip(fields, _FIELD_NAMES, _FIELD_BOUNDS, strict=True):
        if pattern == "*":
            continue
        for part in pattern.split(","):
            if not part:
                return f"{name}字段有空的部分"
            if "/" in part:
                base, _, step_s = part.partition("/")
                if "-" in base:
                    # 后端 _field_matches 会在这里崩，不能放进去
                    return f"{name}字段不支持「范围/步长」（{part}）"
                if not step_s.isdigit() or int(step_s) < 1:
                    return f"{name}字段的步长不合法（{part}）"
                if base != "*" and not _in_range(base, lo, hi):
                    return f"{name}字段超出 {lo}-{hi}（{part}）"
            elif "-" in part:
                a, _, b = part.partition("-")
                if not _in_range(a, lo, hi) or not _in_range(b, lo, hi):
                    return f"{name}字段超出 {lo}-{hi}（{part}）"
                if int(a) > int(b):
                    return f"{name}字段的范围反了（{part}）"
            elif not _in_range(part, lo, hi):
                return f"{name}字段超出 {lo}-{hi}（{part}）"
    return None


def _in_range(text: str, lo: int, hi: int) -> bool:
    return text.isdigit() and lo <= int(text) <= hi


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
