"""常驻调度 daemon — UI 关闭后定时任务继续跑，由 launchd 保活。"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from cli import platform_lock

logger = logging.getLogger(__name__)

WYCKOFF_DIR = Path.home() / ".wyckoff"
LOCK_PATH = WYCKOFF_DIR / "daemon.lock"
LOG_PATH = WYCKOFF_DIR / "logs" / "daemon.log"

_stopped = False


class DaemonLockBusy(RuntimeError):
    """另一个 daemon 已持锁。"""


@contextmanager
def single_instance_lock(lock_path: Path | None = None) -> Iterator[None]:
    """内核级文件锁而非 PID 文件：PID 会被系统回收，导致误判进程还活着。"""
    path = lock_path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open(platform_lock.lock_mode())
    try:
        if not platform_lock.try_acquire(handle):
            raise DaemonLockBusy(f"daemon already running (lock held: {path})")
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            yield
        finally:
            platform_lock.release(handle)
    finally:
        handle.close()


def is_daemon_running(lock_path: Path | None = None) -> bool:
    """探测锁是否被别人持有，用于 TUI 决定是否让出调度权。"""
    path = lock_path or LOCK_PATH
    if not path.exists():
        return False
    try:
        handle = path.open("a+")
    except OSError:
        return False
    try:
        if not platform_lock.try_acquire(handle):
            return True
        platform_lock.release(handle)
        return False
    finally:
        handle.close()


def read_lock_pid(lock_path: Path | None = None) -> int | None:
    path = lock_path or LOCK_PATH
    try:
        return int((path.read_text(encoding="utf-8") or "").strip())
    except (OSError, ValueError):
        return None


def setup_logging(log_path: Path | None = None, *, verbose: bool = False) -> None:
    path = log_path or LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(str(path), when="midnight", backupCount=14, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)


def _handle_signal(signum: int, _frame: object) -> None:
    global _stopped
    _stopped = True
    logger.info("received signal %s, shutting down", signum)


def install_signal_handlers() -> None:
    """Windows 只支持 SIGTERM/SIGINT 的有限语义，SIGBREAK 才是 taskkill 的送达信号。"""
    signals = [signal.SIGTERM, signal.SIGINT]
    if platform_lock.IS_WINDOWS:  # pragma: no cover - platform-specific
        signals.append(getattr(signal, "SIGBREAK", signal.SIGTERM))
    for sig in signals:
        try:
            signal.signal(sig, _handle_signal)
        except (OSError, ValueError):
            logger.debug("cannot install handler for signal %s", sig)


def run_due_schedules(last_check_at: datetime | None, now: datetime) -> datetime:
    """跑本轮到期任务，返回新的检查时间戳。"""
    from cli.approval_queue import expire_stale
    from cli.headless import run_once
    from cli.scheduler import due_schedules, load_schedules, save_schedules

    expired = expire_stale()
    if expired:
        logger.info("expired %d stale approvals", expired)

    schedules = load_schedules()
    due = due_schedules(schedules, last_check_at=last_check_at, now=now)
    if not due:
        return now

    for sched, minute_key in due:
        sched.last_fired = minute_key
        logger.info("firing schedule %s (%s)", sched.id, sched.name)
        result = run_once(sched.action, source="daemon", schedule_id=sched.id)
        sched.last_status = "ok" if result.ok else "failed"
        sched.last_error = "" if result.ok else result.error[:500]
        if result.queued:
            logger.info("schedule %s queued %d approvals", sched.id, len(result.queued))
        if not result.ok:
            logger.warning("schedule %s failed: %s", sched.id, result.error)

    save_schedules(schedules)
    return now


def main_loop(*, lock_path: Path | None = None, poll_seconds: float = 60.0) -> int:
    global _stopped
    _stopped = False
    install_signal_handlers()

    try:
        with single_instance_lock(lock_path):
            logger.info("daemon started (pid=%s)", os.getpid())
            last_check_at: datetime | None = None
            while not _stopped:
                try:
                    last_check_at = run_due_schedules(last_check_at, datetime.now())
                except Exception:
                    logger.exception("schedule tick failed")
                _sleep_interruptible(poll_seconds)
            logger.info("daemon stopped cleanly")
            return 0
    except DaemonLockBusy as exc:
        logger.error("%s", exc)
        return 1


def _sleep_interruptible(seconds: float, *, step: float = 0.5) -> None:
    """分片睡眠，让 SIGTERM 能在一秒内被响应，launchd 才能干净重启。"""
    waited = 0.0
    while waited < seconds and not _stopped:
        time.sleep(min(step, seconds - waited))
        waited += step


def status() -> dict[str, object]:
    running = is_daemon_running()
    return {
        "running": running,
        "pid": read_lock_pid() if running else None,
        "lock": str(LOCK_PATH),
        "log": str(LOG_PATH),
    }
