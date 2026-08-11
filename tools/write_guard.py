"""写操作准入 — MCP 等无审批 UI 的入口默认不得执行高风险写入。

CLI 的 ToolRegistry 有确认弹窗和待批准队列（见 cli/approval_policy.py）；
MCP server 走 ToolSurface，没有任何人机确认环节。默认拒绝而不是默认放行，
否则任何 MCP 客户端都能直接清仓。
"""

from __future__ import annotations

import os

# 与 cli.tools.TOOL_SPECS 里 requires_approval=True 的工具保持一致。
# 此处独立列出，因为 tools/ 不能依赖 cli/（见架构边界测试）。
WRITE_TOOLS = frozenset(
    {
        "update_portfolio",
        "set_stop_loss",
        "record_trade_fill",
        "exec_command",
        "write_file",
    }
)

ALLOW_ENV = "WYCKOFF_MCP_ALLOW_WRITES"

_DENY_MESSAGE = (
    "此入口不支持写操作。MCP 没有审批环节，直接执行会绕过持仓写入的确认流程。"
    "请在 Wyckoff CLI/桌面端里执行，或设置 {env}=1 显式承担风险。"
)


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS


def writes_allowed() -> bool:
    """默认关闭。显式开启才允许无审批入口写入。"""
    return os.getenv(ALLOW_ENV, "").strip().lower() in ("1", "true", "yes")


def check_write_allowed(name: str) -> dict[str, str] | None:
    """返回错误字典表示拒绝；None 表示放行。"""
    if not is_write_tool(name) or writes_allowed():
        return None
    return {"status": "error", "error": _DENY_MESSAGE.format(env=ALLOW_ENV)}
