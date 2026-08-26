"""stdio JSON-RPC 传输 — Electron spawn 这个进程，用管道通信。

stdout 是协议通道，任何写入都会污染它。AGENTS.md 允许 cli/ 用 print() 做用户
输出，而原生扩展还会绕过 Python 直接 write(1, ...)。所以启动时在 fd 层隔离：
协议搬到一个私有 fd，fd 1 改指向 stderr。见 _install_stdout_guard。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, TextIO

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_WORKERS = 4
SHUTDOWN_DRAIN_SECONDS = 2.0

# Windows 管道缓冲和 POSIX 不同；每行显式 flush，否则前端会卡住等不到数据。
_write_lock = threading.Lock()
_protocol_out: TextIO | None = None


def _install_stdout_guard() -> TextIO:
    """把协议通道搬到一个私有 fd，然后让 fd 1 指向 stderr。

    只换 sys.stdout 不够：那只挡得住 Python 层的 print()。pandas / numpy / lxml
    这些原生扩展会直接 write(1, ...)，绕过 sys.stdout 落进协议通道，前端
    JSON.parse 当场失败。而且是概率性的、极难复现 —— 打包分发后在用户机器上
    偶发崩溃，基本无法排查。

    所以在 fd 层做：dup 出一个私有 fd 专供协议，再把 fd 1 dup2 成 stderr。
    这样任何人往 fd 1 写都只会进 stderr，物理上到不了协议通道。
    """
    global _protocol_out
    if _protocol_out is not None:
        return _protocol_out

    try:
        original = sys.stdout.fileno()
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        # stdout/stderr 被替换成非真实文件对象（测试、嵌入式场景）。
        # 退回只换 sys.stdout：仍挡住 print()，但 fd 层无从下手。
        _protocol_out = sys.stdout
        sys.stdout = sys.stderr
        return _protocol_out

    sys.stdout.flush()
    private_fd = os.dup(original)
    _protocol_out = os.fdopen(private_fd, "w", encoding="utf-8", closefd=False)
    # fd 1 从此是 stderr 的别名；C 层直写也只能到日志里。
    os.dup2(stderr_fd, original)
    # sys.stdout 仍指向 fd 1，现在那已经是 stderr，print() 自然去日志。
    sys.stdout = sys.stderr
    return _protocol_out


def _emit(payload: dict[str, Any]) -> None:
    out = _protocol_out or sys.__stdout__
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _write_lock:
        out.write(line + "\n")
        out.flush()


def _respond(request_id: Any, event: dict[str, Any]) -> None:
    # 传输层 id 必须始终是请求流 id。业务事件若也有 id，不能覆盖它；否则前端
    # 无法把事件分发给发起调用的那一轮。业务标识使用 approval_id 等专用字段。
    _emit({**event, "id": request_id})


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
    # 这条路径下有渲染标注的界面；annotate_chart 据此决定是否放行。
    from integrations.chart_annotations import set_renderer_available

    set_renderer_available(True)
    logger.info("ipc stdio server started")
    _emit({"type": "ready", "protocol": PROTOCOL_VERSION})

    try:
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ipc")
        futures = set()
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                if line == "__shutdown__":
                    break
                future = executor.submit(_handle_line, line)
                futures.add(future)
                future.add_done_callback(futures.discard)
        finally:
            wait(futures, timeout=SHUTDOWN_DRAIN_SECONDS)
            executor.shutdown(wait=False, cancel_futures=True)
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        from cli.ipc.session import shutdown_session

        shutdown_session()
        logger.info("ipc stdio server stopped")
    return 0
