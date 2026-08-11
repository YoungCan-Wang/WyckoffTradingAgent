"""无界面跑一轮 Agent — 供 daemon 定时触发和 `wyckoff run` 手动调用。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli import approval_policy, approval_queue

logger = logging.getLogger(__name__)


@dataclass
class HeadlessResult:
    ok: bool
    text: str = ""
    error: str = ""
    queued: list[str] = field(default_factory=list)
    rounds: int = 0


class DaemonGuard:
    """无人监督时的工具闸门：只放行 auto，其余入队等人批。"""

    def __init__(self, *, source: str, schedule_id: str = "", db_path: Path | None = None) -> None:
        self._source = source
        self._schedule_id = schedule_id
        self._db_path = db_path
        self._nav = 0.0
        self.queued: list[str] = []

    def set_nav(self, nav: float) -> None:
        self._nav = nav

    def confirm(self, name: str, args: dict[str, Any]) -> dict[str, str]:
        risk = approval_policy.classify(name, args, self._nav)
        if risk == approval_policy.AUTO:
            return {"action": "allow"}

        approval_id = approval_queue.enqueue(
            name,
            args,
            risk=risk,
            source=self._source,
            schedule_id=self._schedule_id,
            summary=approval_queue.summarize(name, args),
            db_path=self._db_path,
        )
        self.queued.append(approval_id)
        # 说成「用户拒绝」等于伪造一件没发生的事，模型会照着写进回复。
        return {
            "action": "queued",
            "message": (
                f"操作 [{name}] 已加入待批准队列（编号 {approval_id}），尚未执行。"
                "这不是拒绝，也不是失败——等待用户批准。"
                "不要重试该操作，不要声称已完成，直接说明已提交审批。"
            ),
        }

    def ask(self, *_args: Any, **_kwargs: Any) -> str:
        # 后台无人应答；回落到 stdin 会让 daemon 永久挂死。
        from cli.tools import ASK_USER_TIMEOUT_SENTINEL

        return ASK_USER_TIMEOUT_SENTINEL


def build_tools(guard: DaemonGuard) -> Any:
    from cli.tools import ToolRegistry

    tools = ToolRegistry()
    tools.set_confirm_callback(guard.confirm)
    tools.set_ask_user_question_callback(guard.ask)
    return tools


def start_external_mcp(tools: Any) -> Any:
    """接入外部 MCP。写工具仍走 guard，daemon 无人时会入队而非执行。"""
    try:
        from cli.mcp_client import McpClientManager
        from cli.mcp_config import enabled_servers

        servers = enabled_servers()
        if not servers:
            return None
        manager = McpClientManager(servers)
        manager.start()
        if not manager.tools():
            manager.stop()
            return None
        tools.set_mcp_manager(manager)
        return manager
    except Exception:
        logger.warning("external mcp start failed", exc_info=True)
        return None


def current_nav() -> float:
    """取净值用于金额分级；取不到就返回 0，分级会退到 review。"""
    try:
        from agents.portfolio_tools import portfolio

        view = portfolio(mode="view")
    except Exception:
        logger.debug("nav lookup failed", exc_info=True)
        return 0.0
    if not isinstance(view, dict):
        return 0.0
    try:
        return float(view.get("total_equity") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def run_once(
    action: str,
    *,
    source: str = "cli",
    schedule_id: str = "",
    db_path: Path | None = None,
) -> HeadlessResult:
    """跑一轮 agent 到结束。不读 stdin，不依赖任何 UI 对象。"""
    action = action.strip()
    if not action:
        return HeadlessResult(ok=False, error="empty action")

    from cli.bootstrap import build_provider_state, init_local_services, load_config_env

    init_local_services()
    load_config_env()

    state = build_provider_state()
    provider = state.get("provider")
    if provider is None:
        return HeadlessResult(ok=False, error="no model provider configured")

    guard = DaemonGuard(source=source, schedule_id=schedule_id, db_path=db_path)
    tools = build_tools(guard)
    guard.set_nav(current_nav())
    mcp_manager = start_external_mcp(tools)

    from cli.runtime import AgentRuntime
    from core.prompts import CHAT_AGENT_SYSTEM_PROMPT

    runtime = AgentRuntime(provider, tools)
    messages = [{"role": "user", "content": action}]

    try:
        return _consume(runtime.run_stream(messages, CHAT_AGENT_SYSTEM_PROMPT), guard)
    except Exception as exc:
        logger.exception("headless turn failed")
        return HeadlessResult(ok=False, error=str(exc), queued=list(guard.queued))
    finally:
        # 外部 server 是子进程，一轮结束必须回收，否则 daemon 每次触发都泄漏一批。
        if mcp_manager is not None:
            mcp_manager.stop()


def _consume(events: Any, guard: DaemonGuard) -> HeadlessResult:
    text = ""
    rounds = 0
    for event in events:
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "done":
            text = str(event.get("text") or "")
            rounds = int(event.get("rounds") or 0)
            return HeadlessResult(ok=True, text=text, queued=list(guard.queued), rounds=rounds)
        if kind == "turn_cancelled":
            return HeadlessResult(ok=False, error="cancelled", queued=list(guard.queued))
        if kind == "turn_failed":
            return HeadlessResult(
                ok=False,
                error=str(event.get("error") or "turn failed"),
                queued=list(guard.queued),
            )
    return HeadlessResult(ok=False, error="stream ended without terminal event", queued=list(guard.queued))
