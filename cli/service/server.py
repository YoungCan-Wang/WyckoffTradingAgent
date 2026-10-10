"""会员车道的 Agent Runtime 常驻服务。

两种部署形态，网关负责登录态、会员判定和配额，这里只校验网关令牌并把 RuntimeEvent 以 SSE 流出：
- 独占（默认）：一个进程只服务一个用户（容器按用户路由），首个用户把实例钉死，其余 409。
- 共用（WYCKOFF_SERVICE_SHARED=1）：多个用户共用一个进程。每用户同时只跑一轮、全局并发有上限；
  每个请求有自己的工具注册表和请求级凭据，每轮在全新的 contextvars 上下文里跑；
  已知不能共用的工具（命令、文件、浏览器、本机数据库）启动时直接拒绝。

内部协议（不直接暴露给浏览器）：
  GET  /healthz
  POST /v1/turns     {"text", "session_id"?, "llm": {provider_name, api_key, model?, base_url?}}
                     每个 RuntimeEvent 一帧 SSE；会话历史存在进程内存里
  POST /v1/ui-turns  {"messages": UIMessage[], "llm": {...}}
                     ai-sdk UI message stream v1，供 useChat 直接消费；无状态，历史取自 messages
  POST /v1/tools/<name>  {"args": {...}, "credentials": {...}}  ->  {"result": ...}
                     只执行 WYCKOFF_SERVICE_TOOLS 白名单里的工具；循环留在调用方，这里只当工具后端
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import hmac
import json
import logging
import os
import re
import signal
import threading
import time
from collections.abc import Callable, Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from cli.runtime import AgentRuntime
from cli.service.echo_provider import EchoProvider
from cli.service.tool_cache import ToolResultCache
from cli.service.ui_stream import MAX_TEXT_CHARS, UI_STREAM_HEADERS, history_from_ui_messages, tool_output, ui_frames
from cli.tools import ToolRegistry
from core.prompts import CHAT_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

TOKEN_ENV = "AGENT_SERVICE_TOKEN"
ECHO_ENV = "WYCKOFF_SERVICE_ECHO"
TOOLS_ENV = "WYCKOFF_SERVICE_TOOLS"
TURN_SECONDS_ENV = "WYCKOFF_SERVICE_TURN_SECONDS"
SHARED_ENV = "WYCKOFF_SERVICE_SHARED"
MAX_TURNS_ENV = "WYCKOFF_SERVICE_MAX_TURNS"
USER_HEADER = "X-Wyckoff-User"
MAX_BODY_BYTES = 256 * 1024
MAX_SESSIONS = 8
MAX_ECHO_DELAY_MS = 1000
DEFAULT_MAX_TURN_SECONDS = 600.0
DEFAULT_MAX_CONCURRENT_TURNS = 4
DEFAULT_MAX_CONCURRENT_TOOL_CALLS = 6
_TOOL_PATH = re.compile(r"^/v1/tools/([a-z][a-z0-9_]{0,63})$")
MAX_CREDENTIALS = 20
MAX_CREDENTIAL_CHARS = 4096

# 共用进程里绝不能启用的工具（见 docs/CLOUD_TOOL_AUDIT.md）：能跑命令 / 读写任意文件 / 开浏览器，
# 或读写服务器本机的 SQLite 与家目录。其余工具要逐个确认后才能放行，这里只拦已知的。
SHARED_FORBIDDEN_TOOLS = frozenset(
    {
        "exec_command",
        "read_file",
        "write_file",
        "browser_research",
        "app_browser",
        "annotate_chart",
        "execute_skill",
        "render_dashboard",
        "save_report",
        "portfolio",
        "update_portfolio",
        "set_stop_loss",
        "record_trade_fill",
        "query_history",
        "research_hypothesis",
        "diagnose_backend",
    }
)

ProviderFactory = Callable[[dict[str, Any]], tuple[Any, str | None]]


class CloudToolRegistry(ToolRegistry):
    """云端只暴露显式放行的工具，默认一个都不放。

    不走 AgentRuntime(allowed_tools=...)：ToolRegistry.schemas 把空集合当成「不限制」，
    空白名单会把全部工具 schema 展示给模型。这里在注册表一层过滤，展示与存在性校验同源。
    """

    def __init__(self, cloud_tools: frozenset[str], state: dict[str, Any] | None = None) -> None:
        self._cloud_tools = cloud_tools
        super().__init__()
        if state:
            self.state.update(state)

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


def validate_shared_tools(names: frozenset[str]) -> None:
    forbidden = sorted(names & SHARED_FORBIDDEN_TOOLS)
    if forbidden:
        raise ValueError(f"these tools cannot run in a shared process: {', '.join(forbidden)}")


def parse_credentials(value: Any) -> dict[str, str] | None:
    """网关按请求注入的凭据：{字符串: 字符串}，数量和长度有上限；缺省视为没有凭据。"""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_CREDENTIALS:
        return None
    if not all(isinstance(k, str) and isinstance(v, str) and len(v) <= MAX_CREDENTIAL_CHARS for k, v in value.items()):
        return None
    return dict(value)


def in_fresh_context(events: Generator[dict[str, Any], None, None]) -> Generator[dict[str, Any], None, None]:
    """每轮在一个全新的 contextvars 上下文里推进事件流。

    ContextVar 的写入（比如 tushare 的运行期 token）只留在本轮的上下文里。处理线程在保持连接上
    会接着处理下一个请求，不隔离的话上一个用户的 token 会漏给下一个。
    """
    context = contextvars.Context()
    with contextlib.closing(events):
        while True:
            try:
                event = context.run(next, events)
            except StopIteration:
                return
            yield event


class ServiceState:
    def __init__(
        self,
        token: str,
        tools: Any,
        provider_factory: ProviderFactory = default_provider_factory,
        max_turn_seconds: float = DEFAULT_MAX_TURN_SECONDS,
        *,
        shared: bool = False,
        max_concurrent_turns: int | None = None,
        tools_factory: Callable[[str, dict[str, str]], Any] | None = None,
        tool_names: frozenset[str] = frozenset(),
        max_concurrent_tool_calls: int = DEFAULT_MAX_CONCURRENT_TOOL_CALLS,
        tool_cache: ToolResultCache | None = None,
    ) -> None:
        self.token = token
        self.tools = tools
        self.provider_factory = provider_factory
        self.max_turn_seconds = max_turn_seconds
        self.shared = shared
        self.max_concurrent_turns = max_concurrent_turns or (DEFAULT_MAX_CONCURRENT_TURNS if shared else 1)
        self.tools_factory = tools_factory
        self.tool_names = tool_names
        self.tool_cache = tool_cache if tool_cache is not None else ToolResultCache()
        self._tool_slots = threading.BoundedSemaphore(max_concurrent_tool_calls)
        self._lock = threading.Lock()
        self._bound_user: str | None = None
        self._active: set[str] = set()
        self._sessions: dict[tuple[str, str], list[dict[str, Any]]] = {}

    @property
    def active_turns(self) -> int:
        with self._lock:
            return len(self._active)

    def bind_user(self, user: str) -> bool:
        """独占模式：进程第一次见到的用户就是它的主人，网关路由出错时别的用户进不来。共用模式不绑定。"""
        if self.shared:
            return True
        with self._lock:
            if self._bound_user is None:
                self._bound_user = user
            return self._bound_user == user

    def begin_turn(self, user: str) -> str | None:
        """占一个名额；拿不到就返回原因。每个用户同时只跑一轮，全局并发有上限。"""
        with self._lock:
            if user in self._active:
                return "a turn is already running"
            if len(self._active) >= self.max_concurrent_turns:
                return "service is at capacity"
            self._active.add(user)
        return None

    def end_turn(self, user: str) -> None:
        with self._lock:
            self._active.discard(user)

    def history(self, user: str, session_id: str) -> list[dict[str, Any]]:
        key = (user, session_id)
        with self._lock:
            if key not in self._sessions and len(self._sessions) >= MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            return self._sessions.setdefault(key, [])

    def begin_tool_call(self) -> bool:
        """工具调用很短，不占「每用户一轮」的名额，只受全局并发上限约束。"""
        return self._tool_slots.acquire(blocking=False)

    def end_tool_call(self) -> None:
        self._tool_slots.release()

    def tools_for(self, user: str, credentials: dict[str, str]) -> Any:
        """共用模式下每个请求一个注册表，凭据只活在这个请求里；独占模式沿用启动时建好的那个。"""
        return self.tools_factory(user, credentials) if self.tools_factory else self.tools


def iter_turn_events(
    state: ServiceState,
    provider: Any,
    history: list[dict[str, Any]],
    text: str,
    tools: Any,
) -> Generator[dict[str, Any], None, None]:
    history.append({"role": "user", "content": text})
    # 断开信号不一定能穿过网关与容器之间的代理链，所以每轮自带时限作后备。
    # 检查点是流的空档、轮次边界和工具批次，不会打断一段连续输出。
    deadline = time.monotonic() + state.max_turn_seconds
    runtime = AgentRuntime(provider, tools, cancel_check=lambda: time.monotonic() >= deadline)
    for event in runtime.run_stream(history, CHAT_AGENT_SYSTEM_PROMPT):
        yield event
        if event.get("type") == "done" and event.get("text"):
            history.append({"role": "assistant", "content": event["text"]})


def event_frames(events: Generator[dict[str, Any], None, None]) -> Generator[bytes, None, None]:
    with contextlib.closing(events):
        for event in events:
            yield b"data: " + json.dumps(event, ensure_ascii=False, default=str).encode() + b"\n\n"


_WIRES = {"/v1/turns": "events", "/v1/ui-turns": "ui"}


class _AtCapacity(Exception):
    """工具调用并发已满。"""


class _Handler(BaseHTTPRequestHandler):
    # 分块传输要求 HTTP/1.1；SSE 靠它边生成边下发。
    protocol_version = "HTTP/1.1"
    state: ServiceState
    _user: str = ""

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        tool = _TOOL_PATH.match(self.path)
        wire = _WIRES.get(self.path)
        if wire is None and tool is None:
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
        if tool is not None:
            self._run_tool(tool.group(1), body)
        else:
            self._run_turn(body, wire)

    def _admit(self) -> tuple[int, dict[str, str]] | None:
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), self.state.token.encode()):
            return 401, {"error": "unauthorized"}
        user = self.headers.get(USER_HEADER, "").strip()
        if not user:
            return 400, {"error": f"missing {USER_HEADER}"}
        if not self.state.bind_user(user):
            return 409, {"error": "this instance belongs to another user"}
        self._user = user
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

    def _parse_turn(self, body: dict[str, Any], wire: str) -> tuple[str, list[dict[str, Any]]] | None:
        if wire == "ui":
            return history_from_ui_messages(body.get("messages"))
        text = body.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= MAX_TEXT_CHARS:
            return None
        return text, self.state.history(self._user, str(body.get("session_id") or "default"))

    def _run_turn(self, body: dict[str, Any], wire: str) -> None:
        parsed = self._parse_turn(body, wire)
        credentials = parse_credentials(body.get("credentials"))
        if parsed is None or credentials is None:
            self._json(400, {"error": "invalid turn request"})
            return
        text, history = parsed
        provider = self._provider(body)
        if provider is None:
            return
        busy = self.state.begin_turn(self._user)
        if busy is not None:
            self._json(429, {"error": busy})
            return
        try:
            tools = self.state.tools_for(self._user, credentials)
            events = in_fresh_context(iter_turn_events(self.state, provider, history, text, tools))
            if wire == "ui":
                self._stream(ui_frames(events), UI_STREAM_HEADERS)
            else:
                self._stream(event_frames(events))
        except Exception:
            logger.exception("turn setup failed")
            self._json(500, {"error": "turn setup failed"})
        finally:
            self.state.end_turn(self._user)

    def _run_tool(self, name: str, body: dict[str, Any]) -> None:
        args = body.get("args", {})
        credentials = parse_credentials(body.get("credentials"))
        if not isinstance(args, dict) or credentials is None:
            self._json(400, {"error": "invalid tool request"})
            return
        if name not in self.state.tool_names:
            self._json(404, {"error": "unknown tool"})
            return

        # 名额只在真正要算的时候才占：缓存命中和等待同一个结果的请求不占。
        def compute() -> Any:
            if not self.state.begin_tool_call():
                raise _AtCapacity
            try:
                registry = self.state.tools_for(self._user, credentials)
                # 在全新的 contextvars 上下文里执行：运行期 token 之类的写入不会漏给下一个请求。
                return contextvars.Context().run(registry.execute, name, args)
            finally:
                self.state.end_tool_call()

        try:
            result, cached = self.state.tool_cache.get_or_compute(name, args, compute)
            self._json(200, {"result": tool_output(result), "cached": cached})
        except _AtCapacity:
            self._json(429, {"error": "service is at capacity"})
        except Exception:
            logger.exception("tool call failed")
            self._json(500, {"error": "tool call failed"})

    def _provider(self, body: dict[str, Any]) -> Any:
        llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
        try:
            provider, error = self.state.provider_factory(llm)
        except Exception as exc:
            # 异常文本可能带着 key 的片段，只记类型。
            logger.warning("provider init failed: %s", type(exc).__name__)
            provider, error = None, "provider init failed"
        if provider is None:
            self._json(400, {"error": error or "provider unavailable"})
        return provider

    def _stream(self, frames: Generator[bytes, None, None], headers: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            for frame in frames:
                try:
                    self._write_chunk(frame)
                except OSError:
                    # 客户端走了就别再烧模型：关闭生成器，runtime 随之收尾。
                    frames.close()
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
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode()
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


def build_state(token: str) -> ServiceState:
    names = _tool_names()
    seconds = float(os.getenv(TURN_SECONDS_ENV) or DEFAULT_MAX_TURN_SECONDS)
    if os.getenv(SHARED_ENV) != "1":
        return ServiceState(token, CloudToolRegistry(names), max_turn_seconds=seconds, tool_names=names)
    validate_shared_tools(names)
    return ServiceState(
        token,
        None,
        max_turn_seconds=seconds,
        shared=True,
        tool_names=names,
        max_concurrent_turns=int(os.getenv(MAX_TURNS_ENV) or DEFAULT_MAX_CONCURRENT_TURNS),
        tools_factory=lambda user, credentials: CloudToolRegistry(names, {"user_id": user, "credentials": credentials}),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Wyckoff agent runtime service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = parser.parse_args(argv)
    token = os.getenv(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} must be set")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        state = build_state(token)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    server = make_server(args.host, args.port, state)
    # 容器里 python 是 PID 1，没有处理器的 SIGTERM 会被忽略，docker stop 要干等到 SIGKILL。
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    logger.info(
        "agent service listening on %s:%s (%s)", args.host, args.port, "shared" if state.shared else "single-user"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
