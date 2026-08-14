"""常驻会话 — agent 栈只初始化一次。

冷启动实测约 6 秒（pandas/supabase/工具注册表），常驻后每轮对话零启动开销。
这是选常驻 IPC 而非每次 spawn CLI 的唯一理由。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

# 与 TUI 一致：多轮对话保留在内存里，前端不必每次重传历史。
MAX_HISTORY_MESSAGES = 80

_session: DesktopSession | None = None
_session_lock = threading.RLock()


class DesktopSession:
    """一个 IPC 进程内的对话状态。审批闸门把写操作交给前端确认。"""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._tools: Any = None
        self._provider: Any = None
        self._mcp_manager: Any = None
        self._pending_confirms: list[dict[str, Any]] = []
        self._user_id = ""
        self._turn_lock = threading.RLock()
        self.ready_error = ""

    # -- 初始化 ---------------------------------------------------------------

    def start(self) -> None:
        from cli.bootstrap import build_provider_state, init_local_services, load_config_env
        from cli.tools import ToolRegistry

        init_local_services()
        try:
            load_config_env()
        except Exception:
            logger.debug("config env load failed", exc_info=True)

        state = build_provider_state()
        self._provider = state.get("provider")
        if self._provider is None:
            self.ready_error = "no model provider configured"

        session = _load_session()
        self._user_id = str(session.get("user_id") or "")
        self._tools = ToolRegistry(
            user_id=self._user_id,
            access_token=str(session.get("access_token") or ""),
            refresh_token=str(session.get("refresh_token") or ""),
        )
        if self._provider is not None:
            self._tools.set_provider(self._provider)
        # 桌面端有 UI，但确认要走前端；这里先入队，由 approve_decide 执行。
        self._tools.set_confirm_callback(self._confirm)
        self._tools.set_ask_user_question_callback(self._ask)
        self._start_mcp()

    def _start_mcp(self) -> None:
        try:
            from cli.mcp_client import McpClientManager
            from cli.mcp_config import enabled_servers

            servers = enabled_servers()
            if not servers:
                return
            manager = McpClientManager(servers)
            manager.start()
            if manager.tools():
                self._mcp_manager = manager
                self._tools.set_mcp_manager(manager)
            else:
                manager.stop()
        except Exception:
            logger.warning("external mcp start failed", exc_info=True)

    def stop(self) -> None:
        with self._turn_lock:
            if self._mcp_manager is not None:
                try:
                    self._mcp_manager.stop()
                except Exception:
                    logger.debug("mcp stop failed", exc_info=True)
                self._mcp_manager = None

    # -- 工具闸门 -------------------------------------------------------------

    def _confirm(self, name: str, args: dict[str, Any]) -> dict[str, str]:
        """写操作入待批准队列，由前端的审批卡决定，不阻塞当前轮。"""
        from cli import approval_queue as aq
        from cli.approval_policy import classify, explain, nav_ratio

        nav = self._nav()
        risk = classify(name, args, nav)
        approval_id = aq.enqueue(
            name,
            args,
            risk=risk,
            source="desktop",
            summary=aq.summarize(name, args),
            user_id=self._user_id,
            risk_reason=explain(name, args, nav),
            nav_ratio=nav_ratio(args, nav),
        )
        self._pending_confirms.append({"id": approval_id, "tool": name, "risk": risk})
        return {
            "action": "queued",
            "message": (
                f"操作 [{name}] 已提交审批（编号 {approval_id}），尚未执行。"
                "这不是拒绝，也不是失败——等待用户在界面上批准。"
                "不要重试，不要声称已完成，直接说明已提交审批。"
            ),
        }

    def _ask(self, *_args: Any, **_kwargs: Any) -> str:
        from cli.tools import ASK_USER_TIMEOUT_SENTINEL

        return ASK_USER_TIMEOUT_SENTINEL

    def _nav(self) -> float:
        try:
            from cli.headless import current_nav

            return current_nav()
        except Exception:
            return 0.0

    # -- 对话 -----------------------------------------------------------------

    def run_turn(self, text: str) -> Iterator[dict[str, Any]]:
        with self._turn_lock:
            yield from self._run_turn(text)

    def _run_turn(self, text: str) -> Iterator[dict[str, Any]]:
        if self._tools is None:
            self.start()
        if self._provider is None:
            yield {"type": "error", "code": "no_provider", "message": self.ready_error}
            return

        from cli.ipc.tone import tone_suffix
        from cli.runtime import AgentRuntime
        from core.prompts import CHAT_AGENT_SYSTEM_PROMPT
        from integrations.local_auth import load_config

        self._pending_confirms.clear()
        self._messages.append({"role": "user", "content": text})
        runtime = AgentRuntime(self._provider, self._tools)

        # 每轮重读配置：用户可能刚在设置里改了语气，不该等到重启才生效。
        config = load_config()
        prompt = CHAT_AGENT_SYSTEM_PROMPT + tone_suffix(
            str(config.get("desktop_tone") or "default"),
            str(config.get("desktop_tone_custom") or ""),
        )

        try:
            for event in runtime.run_stream(list(self._messages), prompt):
                yield _project(event)
                if isinstance(event, dict) and event.get("type") == "done":
                    reply = str(event.get("text") or "")
                    if reply:
                        self._messages.append({"role": "assistant", "content": reply})
                    self._trim()
        except Exception as exc:
            logger.exception("chat turn failed")
            yield {"type": "error", "code": "turn_failed", "message": str(exc)}

        for pending in self._pending_confirms:
            yield {"type": "approval_pending", **pending}

    def _trim(self) -> None:
        if len(self._messages) > MAX_HISTORY_MESSAGES:
            self._messages = self._messages[-MAX_HISTORY_MESSAGES:]

    def reset(self) -> None:
        self._messages.clear()


# 只透传前端要渲染的字段，避免把内部结构和凭据带出去。
_PASSTHROUGH = {
    "text_delta": ("text",),
    "thinking_delta": ("text",),
    "tool_start": ("name", "display_name", "args"),
    "tool_calls": ("names",),
    "tool_error": ("name", "error"),
    "model_start": ("model",),
    "usage": ("input_tokens", "output_tokens"),
    "retry": ("reason",),
    "done": ("text", "rounds", "elapsed"),
    "turn_failed": ("error",),
    "turn_cancelled": (),
}


def _project(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {"type": "unknown"}
    kind = str(event.get("type") or "unknown")
    fields = _PASSTHROUGH.get(kind)
    if fields is None:
        return {"type": kind}
    return {"type": kind, **{k: event.get(k) for k in fields if event.get(k) is not None}}


def _load_session() -> dict[str, Any]:
    try:
        from cli.auth import load_session

        return load_session() or {}
    except Exception:
        logger.debug("load session failed", exc_info=True)
        return {}


def get_session() -> DesktopSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = DesktopSession()
            _session.start()
        return _session


def shutdown_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
            _session = None
