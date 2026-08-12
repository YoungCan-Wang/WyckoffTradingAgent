"""跨平台排他文件锁 — daemon 单例保护。

用文件锁而非 PID 文件：PID 会被系统回收，导致误判进程还活着。
POSIX 用 fcntl.flock，Windows 用 msvcrt.locking —— 两者都是内核级锁，
进程崩溃时由操作系统自动释放，不会留下需要人工清理的僵尸锁。
"""

from __future__ import annotations

import sys
from typing import IO

IS_WINDOWS = sys.platform == "win32"

# 两个模块都按可选处理：任何一边缺失都不该让 import cli.daemon 直接失败，
# 否则 `wyckoff daemon` 在该平台上连启动都做不到。
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]

_USE_MSVCRT = msvcrt is not None and fcntl is None

# Windows 的 msvcrt.locking 需要显式字节数；锁首字节即可实现互斥。
_LOCK_BYTES = 1


class LockBusy(RuntimeError):
    """锁已被其他进程持有。"""


def available() -> bool:
    """当前平台是否有可用的锁实现。"""
    return fcntl is not None or msvcrt is not None


def try_acquire(handle: IO) -> bool:
    """尝试非阻塞加锁。成功返回 True，已被占用返回 False。"""
    if not available():
        raise RuntimeError("no file-locking primitive available on this platform")
    try:
        if _USE_MSVCRT:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def release(handle: IO) -> None:
    """释放锁；已经释放或句柄失效时静默返回。"""
    try:
        if _USE_MSVCRT:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
        elif fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def lock_mode() -> str:
    """Windows 的 msvcrt.locking 要求句柄可读写，POSIX 只需可写。"""
    return "a+" if _USE_MSVCRT else "w"
