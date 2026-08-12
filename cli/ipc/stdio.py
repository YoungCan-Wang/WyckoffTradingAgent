"""stdio JSON-RPC 传输 — Electron spawn 这个进程，用管道通信。

stdout 是协议通道，任何 print() 都会污染它。AGENTS.md 允许 cli/ 用 print()
做用户输出，所以启动时必须把 sys.stdout 换掉：业务代码继续 print，
内容被重定向到 stderr，只有协议行能走真正的 stdout。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any, TextIO

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# Windows 管道缓冲和 POSIX 不同；每行显式 flush，否则前端会卡住等不到数据。
_write_lock = threading.Lock()
_protocol_out: TextIO | None = None


def _install_stdout_guard() -> TextIO:
    """把真正的 stdout 抢下来做协议通道，业务 print() 改去 stderr。"""
    global _protocol_out
    if _protocol_out is not None:
        return _protocol_out
    _protocol_out = sys.stdout
    sys.stdout = sys.stderr
    return _protocol_out


def _emit(payload: dict[str, Any]) -> None:
    out = _protocol_out or sys.__stdout__
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _write_lock:
        out.write(line + "\n")
        out.flush()


def _respond(request_id: Any, event: dict[str, Any]) -> None:
    _emit({"id": request_id, **event})


def _handle_line(raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        _emit({"type": "error", "code": "parse_error", "message": "invalid JSON line"})
        return
    if not isinstance(message, dict):
        _emit({"type": "error", "code": "parse_error", "message": "expected a JSON object"})
        return

    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    from cli.ipc.methods import MethodError, dispatch

    try:
        for event in dispatch(method, params):
            _respond(request_id, event)
        _respond(request_id, {"type": "end"})
    except MethodError as exc:
        _respond(request_id, {"type": "error", "code": exc.code, "message": exc.message})
        _respond(request_id, {"type": "end"})
    except Exception as exc:
        logger.exception("ipc method %s failed", method)
        _respond(request_id, {"type": "error", "code": "internal", "message": str(exc)})
        _respond(request_id, {"type": "end"})


def _setup_logging(verbose: bool) -> None:
    from pathlib import Path

    log_dir = Path.home() / ".wyckoff" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "ipc.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)


def serve(*, verbose: bool = False) -> int:
    """读 stdin 每一行，处理后把事件写回 stdout。EOF 即退出。"""
    _install_stdout_guard()
    _setup_logging(verbose)
    logger.info("ipc stdio server started")
    _emit({"type": "ready", "protocol": PROTOCOL_VERSION})

    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            if line == "__shutdown__":
                break
            _handle_line(line)
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        from cli.ipc.session import shutdown_session

        shutdown_session()
        logger.info("ipc stdio server stopped")
    return 0
