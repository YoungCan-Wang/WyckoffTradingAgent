"""Unified pre-execution gates for tool calls (prepareToolCall)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PrepareAction = Literal["accept", "reject", "rewrite"]


@dataclass(frozen=True)
class PrepareDecision:
    action: PrepareAction
    args: dict[str, Any] = field(default_factory=dict)
    code: str = ""
    message: str = ""
    details: dict[str, Any] | None = None

    def error_result(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.details:
            payload["details"] = self.details
        return payload


def accept(args: dict[str, Any] | None = None) -> PrepareDecision:
    return PrepareDecision(action="accept", args=dict(args or {}))


def reject(
    code: str,
    message: str,
    *,
    args: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> PrepareDecision:
    return PrepareDecision(
        action="reject",
        args=dict(args or {}),
        code=code,
        message=message,
        details=details,
    )


def rewrite(args: dict[str, Any], *, code: str = "rewritten", message: str = "") -> PrepareDecision:
    return PrepareDecision(action="rewrite", args=dict(args), code=code, message=message)


def prepare_allowed_tools(name: str, args: dict[str, Any], allowed_tools: set[str] | None) -> PrepareDecision | None:
    if allowed_tools is not None and name not in allowed_tools:
        return reject(
            "tool_not_allowed",
            f"工具 {name} 不在当前 workflow/对话允许范围内",
            args=args,
        )
    return None


def prepare_exists(name: str, args: dict[str, Any], *, known: bool) -> PrepareDecision | None:
    if not known:
        return reject("tool_not_found", f"未知工具: {name}", args=args)
    return None
