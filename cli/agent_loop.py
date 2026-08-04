"""Agent loop continuation policy (aligned with web chat-agent-loop)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_AUTO_CONTINUATIONS = 2
MAX_TOTAL_TOOL_ROUNDS = 32

CONTINUATION_PROMPT = (
    "继续完成当前分析。复用已经完成的工具结果，不要重复已完成的数据读取；如果关键数据已经足够，直接补全并给出完整结论。"
)

AgentContinuationReason = Literal["output-length", "step-limit", "unfinished-work"]


@dataclass(frozen=True)
class AgentLoopDecision:
    kind: Literal["complete", "continue", "error"]
    reason: AgentContinuationReason | None = None
    message: str = ""


def decide_agent_loop(
    *,
    finish_reason: str,
    step_count: int,
    max_steps: int,
    has_tool_calls: bool,
    has_tool_approval: bool = False,
    has_incomplete_tool_call: bool = False,
    unfinished_required_work: bool = False,
) -> AgentLoopDecision:
    """Decide whether the agent should stop, auto-continue, or surface an error."""

    if has_tool_approval:
        return AgentLoopDecision(kind="complete")
    if has_incomplete_tool_call:
        return AgentLoopDecision(
            kind="error",
            message="模型在工具参数尚未完整生成时中断，本轮无法安全续跑。请输入「继续」补齐缺失步骤。",
        )
    if finish_reason in {"length", "max_tokens", "MAX_TOKENS"}:
        return AgentLoopDecision(kind="continue", reason="output-length")
    if unfinished_required_work:
        return AgentLoopDecision(kind="continue", reason="unfinished-work")
    if has_tool_calls and step_count >= max_steps:
        return AgentLoopDecision(kind="continue", reason="step-limit")
    return AgentLoopDecision(kind="complete")


def continuation_limit_message(reason: AgentContinuationReason) -> str:
    if reason == "output-length":
        return "模型输出达到单次上限，自动续写次数已用尽。请输入「继续」完成剩余分析。"
    if reason == "unfinished-work":
        return "仍有未完成的必需工具步骤，自动续跑次数已用尽。请输入「继续」完成剩余分析。"
    return f"本轮工具执行达到 {MAX_TOTAL_TOOL_ROUNDS} 步上限附近，自动续跑次数已用尽。请输入「继续」完成剩余分析。"


def has_incomplete_tool_calls(tool_calls: list[dict] | None) -> bool:
    if not tool_calls:
        return False
    for call in tool_calls:
        name = str(call.get("name") or "").strip()
        if not name:
            return True
    return False
