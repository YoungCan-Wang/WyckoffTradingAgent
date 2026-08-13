"""Execute one previously reviewed approval through the normal tool registry."""

from __future__ import annotations

from typing import Any


def execute_approved(
    tool_name: str,
    args: dict[str, Any],
    *,
    expected_user_id: str | None = None,
) -> Any:
    from cli.auth import load_session
    from cli.tools import ToolRegistry

    session = load_session() or {}
    current_user = str(session.get("user_id") or "")
    if expected_user_id is not None and current_user != str(expected_user_id or ""):
        return {"error": "审批项所属账户与当前登录不一致，已拒绝执行"}

    tools = ToolRegistry(
        user_id=current_user,
        access_token=str(session.get("access_token") or ""),
        refresh_token=str(session.get("refresh_token") or ""),
    )
    tools.set_confirm_callback(lambda _name, _args: {"action": "allow"})
    manager = _start_external_mcp(tools) if tool_name.startswith("mcp__") else None
    try:
        prepared = tools.prepare(tool_name, args)
        if prepared.action == "reject":
            return {"error": prepared.message or "待批准参数已失效", "code": prepared.code}
        return tools.execute(tool_name, prepared.args or args)
    finally:
        if manager is not None:
            manager.stop()


def _start_external_mcp(tools: Any) -> Any:
    from cli.mcp_client import McpClientManager
    from cli.mcp_config import enabled_servers

    manager = McpClientManager(enabled_servers())
    manager.start()
    tools.set_mcp_manager(manager)
    return manager
