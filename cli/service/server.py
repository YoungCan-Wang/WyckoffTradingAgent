"""会员车道的 Agent Runtime 常驻服务。

一个进程只服务一个用户（容器按用户路由）。网关负责登录态、会员判定和配额，
这里只做三件事：校验网关令牌、把用户钉死在进程上、把 RuntimeEvent 以 SSE 流出。

内部协议（不直接暴露给浏览器）：
  GET  /healthz
  POST /v1/turns  {"text", "session_id"?, "llm": {provider_name, api_key, model?, base_url?}}
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import signal
import threading
from collections.abc import Callable, Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from cli.runtime import AgentRuntime
from cli.service.echo_provider import EchoProvider
from cli.tools import ToolRegistry
from core.prompts import CHAT_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

TOKEN_ENV = "AGENT_SERVICE_TOKEN"
ECHO_ENV = "WYCKOFF_SERVICE_ECHO"
TOOLS_ENV = "WYCKOFF_SERVICE_TOOLS"
USER_HEADER = "X-Wyckoff-User"
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20_000
MAX_SESSIONS = 8
MAX_ECHO_DELAY_MS = 1000

ProviderFactory = Callable[[dict[str, Any]], tuple[Any, str | None]]


class CloudToolRegistry(ToolRegistry):
    """云端只暴露显式放行的工具，默认一个都不放。

    不走 AgentRuntime(allowed_tools=...)：ToolRegistry.schemas 把空集合当成「不限制」，
    空白名单会把全部工具 schema 展示给模型。这里在注册表一层过滤，展示与存在性校验同源。
    """

    def __init__(self, cloud_tools: frozenset[str]) -> None:
        self._cloud_tools = cloud_tools
        super().__init__()

    def schemas(self, allowed_tools: set[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        return [schema for schema in super().schemas(allowed_tools) if schema["name"] in self._cloud_tools]


def default_provider_factory(llm: dict[str, Any]) -> tuple[Any, str | None]:
    name = str(llm.get("provider_name") or "")
    if name == "echo":
        if os.getenv(ECHO_ENV) != "1":
            return None, "echo provider is disabled"
        delay_ms = min(max(int(llm.get("delay_ms") or 0), 0), MAX_ECHO_DELAY_MS)
        return EchoProvider(delay_ms / 1000), None
    if not name or not llm.get("api_key"):
        return None, "llm.provider_name and llm.api_key are required"
    from cli.provider_factory import create_provider

    return create_provider(name, str(llm["api_key"]), str(llm.get("model") or ""), str(llm.get("base_url") or ""))


class ServiceState:
    def __init__(self, token: str, tools: Any, provider_factory: ProviderFactory = default_provider_factory) -> None:
        self.token = token
        self.tools = tools
        self.provider_factory = provider_factory
        self.turn_gate = threading.Lock()
        self._bind_lock = threading.Lock()
        self._bound_user: str | None = None
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def bind_user(self, user: str) -> bool:
        """进程第一次见到的用户就是它的主人；网关路由出错时别的用户进不来。"""
        with self._bind_lock:
            if self._bound_user is None:
                self._bound_user = user
            return self._bound_user == user

    def history(self, session_id: str) -> list[dict[str, Any]]:
        if session_id not in self._sessions and len(self._sessions) >= MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))
        return self._sessions.setdefault(session_id, [])


def iter_turn_events(
    state: ServiceState,
    provider: Any,
    history: list[dict[str, Any]],
    text: str,
) -> Generator[dict[str, Any], None, None]:
    history.append({"role": "user", "content": text})
    runtime = AgentRuntime(provider, state.tools)
    for event in runtime.run_stream(history, CHAT_AGENT_SYSTEM_PROMPT):
        yield event
        if event.get("type") == "done" and event.get("text"):
            history.append({"role": "assistant", "content": event["text"]})


class _Handler(BaseHTTPRequestHandler):
    # 分块传输要求 HTTP/1.1；SSE 靠它边生成边下发。
    protocol_version = "HTTP/1.1"
    state: ServiceState

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/turns":
            self._json(404, {"error": "not found"})
            return
        rejection = self._admit()
        if rejection is not None:
            self._json(*rejection)
            return
        body = self._read_body()
        if body is None:
            self._json(400, {"error": "invalid json body"})
            return
        self._run_turn(body)

    def _admit(self) -> tuple[int, dict[str, str]] | None:
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), self.state.token.encode()):
            return 401, {"error": "unauthorized"}
        user = self.headers.get(USER_HEADER, "").strip()
        if not user:
            return 400, {"error": f"missing {USER_HEADER}"}
        if not self.state.bind_user(user):
            return 409, {"error": "this instance belongs to another user"}
        return None

    def _read_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY_BYTES:
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _run_turn(self, body: dict[str, Any]) -> None:
        text = body.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= MAX_TEXT_CHARS:
            self._json(400, {"error": "text must be a non-empty string"})
            return
        llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
        try:
            provider, error = self.state.provider_factory(llm)
        except Exception as exc:
            # 异常文本可能带着 key 的片段，只记类型。
            logger.warning("provider init failed: %s", type(exc).__name__)
            provider, error = None, "provider init failed"
        if provider is None:
            self._json(400, {"error": error or "provider unavailable"})
            return
        if not self.state.turn_gate.acquire(blocking=False):
            self._json(429, {"error": "a turn is already running"})
            return
        try:
            history = self.state.history(str(body.get("session_id") or "default"))
            self._stream(iter_turn_events(self.state, provider, history, text))
        finally:
            self.state.turn_gate.release()

    def _stream(self, events: Generator[dict[str, Any], None, None]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event in events:
                frame = "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
                try:
                    self._write_chunk(frame.encode())
                except OSError:
                    # 客户端走了就别再烧模型：关闭生成器，runtime 随之收尾。
                    events.close()
                    self.close_connection = True
                    return
        except Exception:
            logger.exception("turn failed")  # runtime 已先推出 turn_failed 事件，这里只负责体面收尾
        try:
            self._write_chunk(b"")
        except OSError:
            self.close_connection = True

    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status >= 400:
            # 拒绝可能发生在读完请求体之前，连接上可能还留着没读的字节。
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str, port: int, state: ServiceState) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def _tool_names() -> frozenset[str]:
    return frozenset(name.strip() for name in os.getenv(TOOLS_ENV, "").split(",") if name.strip())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Wyckoff agent runtime service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = parser.parse_args(argv)
    token = os.getenv(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} must be set")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = make_server(args.host, args.port, ServiceState(token, CloudToolRegistry(_tool_names())))
    # 容器里 python 是 PID 1，没有处理器的 SIGTERM 会被忽略，docker stop 要干等到 SIGKILL。
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    logger.info("agent service listening on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
