"""外部 MCP server 客户端 — 把第三方工具接进本地工具注册表。

SDK 只有 async API，而 ToolRegistry.execute 是同步的。桥接方式必须是
anyio blocking portal + wrap_async_context_manager：手工 AsyncExitStack
配 portal.call 会炸 "Attempted to exit a cancel scope that isn't the
current task's"，因为 cancel scope 必须在创建它的 task 里退出。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from cli.mcp_config import Server, enabled_servers

logger = logging.getLogger(__name__)

LOG_DIR = Path.home() / ".wyckoff" / "logs"

# 外部 server 的描述会进 system prompt，等于第三方能往模型上下文里写字。
_MAX_DESCRIPTION_CHARS = 600


@dataclass
class ExternalTool:
    name: str
    server: str
    description: str
    input_schema: dict[str, Any]
    is_write: bool

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema or {"type": "object", "properties": {}},
        }


@dataclass
class ServerState:
    server: Server
    session: Any = None
    tools: list[ExternalTool] = field(default_factory=list)
    error: str = ""

    @property
    def available(self) -> bool:
        return self.session is not None and not self.error


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


class McpClientManager:
    """会话期常连：启动时连一次，会话内复用，进程随会话结束回收。"""

    def __init__(self, servers: list[Server] | None = None) -> None:
        self._servers = servers if servers is not None else enabled_servers()
        self._states: dict[str, ServerState] = {}
        self._portal = None
        self._portal_cm = None
        self._stack = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self._servers or self._portal is not None:
            return
        if not mcp_available():
            logger.info("mcp extra not installed; external MCP disabled")
            return
        import anyio.from_thread as from_thread

        self._portal_cm = from_thread.start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        from contextlib import ExitStack

        self._stack = ExitStack()
        for server in self._servers:
            self._connect(server)

    def stop(self) -> None:
        if self._stack is not None:
            try:
                self._stack.close()
            except Exception:
                logger.debug("mcp stack close failed", exc_info=True)
            self._stack = None
        if self._portal_cm is not None:
            try:
                self._portal_cm.__exit__(None, None, None)
            except Exception:
                logger.debug("mcp portal close failed", exc_info=True)
        self._portal = None
        self._portal_cm = None
        self._states.clear()

    def _connect(self, server: Server) -> None:
        state = ServerState(server=server)
        self._states[server.name] = state
        try:
            session = self._stack.enter_context(self._portal.wrap_async_context_manager(_session_cm(server)))
            result = self._portal.call(session.list_tools)
            state.session = session
            state.tools = _build_tools(server, result)
            logger.info("mcp server %s connected, %d tools", server.name, len(state.tools))
        except Exception as e:
            # 连不上只让这个 server 不可用，绝不能影响原生工具。
            state.error = _short(e)
            logger.warning("mcp server %s unavailable: %s", server.name, state.error)

    # -- discovery ---------------------------------------------------------

    def tools(self) -> list[ExternalTool]:
        return [tool for state in self._states.values() if state.available for tool in state.tools]

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools()]

    def find(self, name: str) -> ExternalTool | None:
        return next((tool for tool in self.tools() if tool.name == name), None)

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": state.server.name,
                "available": state.available,
                "tool_count": len(state.tools),
                "error": state.error,
            }
            for state in self._states.values()
        ]

    # -- invocation --------------------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = self.find(name)
        if tool is None:
            return {"error": f"未知的外部工具: {name}"}
        state = self._states.get(tool.server)
        if state is None or not state.available:
            return {"error": f"MCP server {tool.server} 当前不可用"}

        bare = name[len(f"mcp__{tool.server}__") :]
        timeout = timedelta(seconds=state.server.timeout_seconds)
        try:
            result = self._portal.call(lambda: state.session.call_tool(bare, args or {}, read_timeout_seconds=timeout))
        except Exception as e:
            logger.warning("mcp call %s failed: %s", name, _short(e))
            return {"error": f"调用 {name} 失败: {_short(e)}"}
        return _normalize_result(result)


@asynccontextmanager
async def _session_cm(server: Server):
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=server.command,
        args=list(server.args),
        env=dict(server.env) or None,
    )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # server 的 stderr 默认灌到我们的 stderr，会把 Textual 画面搅乱。
    with (LOG_DIR / f"mcp-{server.name}.log").open("a", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=server.timeout_seconds)
            ) as session:
                await session.initialize()
                yield session


def _build_tools(server: Server, result: Any) -> list[ExternalTool]:
    from cli.mcp_policy import is_write_tool

    tools: list[ExternalTool] = []
    for tool in getattr(result, "tools", []) or []:
        bare = getattr(tool, "name", "")
        if not bare:
            continue
        annotations = getattr(tool, "annotations", None)
        tools.append(
            ExternalTool(
                name=f"{server.tool_prefix()}{bare}",
                server=server.name,
                description=_describe(server, bare, tool),
                input_schema=getattr(tool, "inputSchema", None) or {},
                is_write=is_write_tool(bare, annotations),
            )
        )
    return tools


def _describe(server: Server, bare: str, tool: Any) -> str:
    raw = str(getattr(tool, "description", "") or "").strip()
    if len(raw) > _MAX_DESCRIPTION_CHARS:
        raw = raw[:_MAX_DESCRIPTION_CHARS] + "…"
    return f"[外部 MCP: {server.name}] {raw or bare}"


def _normalize_result(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        return {"error": _text_of(result) or "外部工具返回错误"}
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured
    text = _text_of(result)
    if not text:
        return {"result": "(无返回内容)"}
    # 多数 server 只在 text block 里放 JSON，不填 structuredContent。
    # 还原成对象比塞给模型一个 JSON 字符串更好用。
    parsed = _try_json(text)
    return parsed if parsed is not None else {"result": text}


def _try_json(text: str) -> dict[str, Any] | None:
    import json

    if not text.startswith(("{", "[")):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return {"result": value} if isinstance(value, list) else None


def _text_of(result: Any) -> str:
    blocks = getattr(result, "content", None) or []
    return "\n".join(str(getattr(b, "text", "")) for b in blocks if getattr(b, "type", "") == "text").strip()


def _short(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    return message[:200]
