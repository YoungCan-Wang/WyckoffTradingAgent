"""Sub-agent 基础设施 — SubAgent 定义、工具代理、运行函数、委派工具。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from typing import Any

from cli.compaction import estimate_tokens
from cli.runtime import AgentCancelled
from cli.sub_agent_prompts import (
    ANALYSIS_AGENT_PROMPT,
    RESEARCH_AGENT_PROMPT,
    TRADING_AGENT_PROMPT,
    WORKFLOW_TASK_AGENT_PROMPT,
)

logger = logging.getLogger(__name__)


def _sub_agent_stream_chunk_timeout(sub_timeout_seconds: float) -> float:
    from cli.auth import get_stream_chunk_timeout_seconds

    return min(get_stream_chunk_timeout_seconds(), float(sub_timeout_seconds))


@dataclass(frozen=True)
class SubAgent:
    name: str
    system_prompt: str
    tool_names: tuple[str, ...]
    description: str = ""
    timeout_seconds: int = 180
    max_tool_rounds: int = 8
    context_budget_tokens: int = 16_000
    result_budget_chars: int = 2_500
    tool_timeout_seconds: int = 60


RESEARCH_AGENT = SubAgent(
    name="research",
    description="数据收集：全市场扫描、信号、推荐、回测",
    system_prompt=RESEARCH_AGENT_PROMPT,
    timeout_seconds=240,
    max_tool_rounds=8,
    context_budget_tokens=24_000,
    result_budget_chars=3_000,
    tool_timeout_seconds=90,
    tool_names=(
        "search_stock_by_name",
        "analyze_stock",
        "get_market_overview",
        "get_market_history",
        "query_history",
        "evaluate_recommendation_events",
        "screen_stocks",
        "run_backtest",
        "check_background_tasks",
    ),
)

ANALYSIS_AGENT = SubAgent(
    name="analysis",
    description="深度分析：个股诊断、持仓体检、AI 研报",
    system_prompt=ANALYSIS_AGENT_PROMPT,
    timeout_seconds=180,
    max_tool_rounds=8,
    context_budget_tokens=20_000,
    result_budget_chars=2_500,
    tool_timeout_seconds=75,
    tool_names=(
        "analyze_stock",
        "portfolio",
        "get_market_overview",
        "get_market_history",
        "generate_ai_report",
    ),
)

TRADING_AGENT = SubAgent(
    name="trading",
    description="去留决策：攻防指令、调仓计划",
    system_prompt=TRADING_AGENT_PROMPT,
    timeout_seconds=120,
    max_tool_rounds=6,
    context_budget_tokens=12_000,
    result_budget_chars=1_600,
    tool_timeout_seconds=45,
    tool_names=(
        "portfolio",
        "generate_strategy_decision",
        "analyze_stock",
        "get_market_overview",
        "get_market_history",
    ),
)

WORKFLOW_TASK_AGENT = SubAgent(
    name="task",
    description="动态任务：按模型脚本执行单个 workflow task",
    system_prompt=WORKFLOW_TASK_AGENT_PROMPT,
    timeout_seconds=180,
    max_tool_rounds=8,
    context_budget_tokens=20_000,
    result_budget_chars=2_500,
    tool_timeout_seconds=75,
    tool_names=(
        "search_stock_by_name",
        "analyze_stock",
        "portfolio",
        "get_market_overview",
        "get_market_history",
        "query_history",
        "evaluate_recommendation_events",
        "screen_stocks",
        "run_backtest",
        "check_background_tasks",
        "generate_ai_report",
        "generate_strategy_decision",
        "ask_user_question",
    ),
)

_FALLBACK_TOOLS_BY_AGENT = {
    "research": ("get_market_overview", "query_history", "check_background_tasks"),
    "analysis": ("analyze_stock", "portfolio", "get_market_overview"),
    "trading": ("portfolio", "generate_strategy_decision", "get_market_overview"),
}
_WORKFLOW_EXPECTATION_TOOLS = frozenset(
    {
        "analyze_stock",
        "portfolio",
        "get_market_overview",
        "screen_stocks",
        "generate_ai_report",
        "generate_strategy_decision",
        "run_backtest",
    }
)

_POLICY_BY_STATUS = {
    "completed": ("use_result", False, "使用子 Agent 返回的结论。"),
    "timeout": ("fallback_to_direct_tools", True, "不要反复委派；用 fallback_tools 做窄范围降级处理。"),
    "error": ("fallback_to_direct_tools", True, "不要反复委派；用 fallback_tools 获取必要事实后降级回答。"),
    "empty": ("fallback_to_direct_tools", False, "子 Agent 没有产出；用 fallback_tools 做最小可用回答。"),
    "cancelled": ("stop_and_report_cancelled", False, "用户已中断任务，停止继续调用工具。"),
}


class SubAgentToolProxy:
    """限制 sub-agent 只能看到/调用指定工具子集。"""

    def __init__(self, registry, allowed: set[str], *, tool_timeout_seconds: int = 60, deadline: float | None = None):
        self._registry = registry
        self._allowed = allowed
        self._tool_timeout_seconds = max(1, int(tool_timeout_seconds))
        self._deadline = deadline

    def schemas(self) -> list[dict[str, Any]]:
        return [s for s in self._registry.schemas() if s["name"] in self._allowed]

    def execute(self, name: str, args: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Any:
        if name not in self._allowed:
            return {"error": f"sub-agent 无权调用工具: {name}"}
        timeout = self._remaining_tool_timeout()
        if timeout <= 0:
            return {"error": f"sub-agent 工具调用超时: {name}"}
        return _execute_with_timeout(
            lambda: self._registry.execute(name, args, messages=messages),
            timeout,
            name,
        )

    def concurrency_safe(self, name: str) -> bool:
        if name not in self._allowed:
            return False
        return self._registry.concurrency_safe(name)

    def _remaining_tool_timeout(self) -> float:
        timeout = float(self._tool_timeout_seconds)
        if self._deadline is None:
            return timeout
        return min(timeout, self._deadline - time.monotonic())


def _execute_with_timeout(fn: Callable[[], Any], timeout_seconds: float, tool_name: str) -> Any:
    results: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def _target() -> None:
        try:
            results.put((True, fn()))
        except BaseException as exc:
            results.put((False, exc))

    thread = Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return {"error": f"sub-agent 工具调用超时: {tool_name} > {timeout_seconds:.0f}s"}
    try:
        ok, value = results.get_nowait()
    except Empty:
        return {"error": f"sub-agent 工具调用异常: {tool_name} 无返回"}
    if ok:
        return value
    raise value


def run_sub_agent(
    sub: SubAgent,
    task: str,
    context: str,
    provider,
    registry,
    on_progress=None,
    cancel_check: Callable[[], bool] | None = None,
    tool_names: tuple[str, ...] | None = None,
    enforce_turn_expectations: bool = False,
    required_tool_names: tuple[str, ...] | None = None,
    required_tool_args: dict[str, dict[str, str]] | None = None,
    required_tool_arg_sets: dict[str, tuple[dict[str, Any], ...]] | None = None,
) -> dict[str, Any]:
    """启动一个 sub-agent mini loop，通过 on_progress 实时上报事件。"""
    from cli.runtime import AgentRuntime

    started_at = time.monotonic()
    deadline = started_at + max(1, sub.timeout_seconds)
    allowed_tools = _sub_agent_tool_set(sub, tool_names)
    proxy = SubAgentToolProxy(
        registry,
        allowed_tools,
        tool_timeout_seconds=sub.tool_timeout_seconds,
        deadline=deadline,
    )
    trimmed_context, context_truncated = _fit_context(context, sub.context_budget_tokens)
    user_content = f"{task}\n\n上下文:\n{trimmed_context}" if trimmed_context else task
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    tool_calls: list[str] = []
    background_task_ids: list[str] = []
    cancelled = _sub_agent_cancel_check(cancel_check, deadline)

    runtime = AgentRuntime(
        provider,
        proxy,
        max_tool_rounds=sub.max_tool_rounds,
        cancel_check=cancelled,
        stream_chunk_timeout=_sub_agent_stream_chunk_timeout(sub.timeout_seconds),
        allowed_tools=allowed_tools,
        required_tools=required_tool_names if enforce_turn_expectations else None,
        required_tool_args=required_tool_args if enforce_turn_expectations else None,
        required_tool_arg_sets=required_tool_arg_sets if enforce_turn_expectations else None,
        enforce_turn_expectations=_sub_agent_turn_expectations_enabled(
            sub,
            allowed_tools,
            enforce_turn_expectations,
        ),
    )

    return _run_sub_agent_loop(
        sub,
        runtime,
        messages,
        started_at,
        tool_calls,
        background_task_ids,
        context_truncated,
        cancelled,
        deadline,
        on_progress,
    )


def _sub_agent_tool_set(sub: SubAgent, tool_names: tuple[str, ...] | None) -> set[str]:
    if tool_names is None:
        return set(sub.tool_names)
    default_tools = set(sub.tool_names)
    return {name for name in tool_names if name in default_tools}


def _sub_agent_turn_expectations_enabled(sub: SubAgent, allowed_tools: set[str], requested: bool) -> bool:
    return (
        requested
        and sub.name == WORKFLOW_TASK_AGENT.name
        and bool(allowed_tools.intersection(_WORKFLOW_EXPECTATION_TOOLS))
    )


def _run_sub_agent_loop(
    sub: SubAgent,
    runtime,
    messages: list[dict[str, Any]],
    started_at: float,
    tool_calls: list[str],
    background_task_ids: list[str],
    context_truncated: bool,
    cancelled: Callable[[], bool],
    deadline: float,
    on_progress=None,
) -> dict[str, Any]:
    from core.prompts import with_current_time

    try:
        for event in runtime.run_stream(messages, with_current_time(sub.system_prompt)):
            if cancelled():
                raise AgentCancelled()
            if event["type"] == "tool_start":
                tool_calls.append(event["name"])
            if event["type"] == "tool_result":
                background_task_ids.extend(_background_task_ids(event.get("result")))
            if on_progress:
                event["sub_agent"] = sub.name
                on_progress(event)
            if event["type"] == "done":
                return _sub_agent_result(
                    sub, "completed", event, started_at, tool_calls, background_task_ids, context_truncated
                )
    except AgentCancelled:
        status = "timeout" if time.monotonic() >= deadline else "cancelled"
        return _sub_agent_result(
            sub,
            status,
            {},
            started_at,
            tool_calls,
            background_task_ids,
            context_truncated,
            error=f"sub-agent {status}",
        )
    except TimeoutError as exc:
        return _sub_agent_result(
            sub, "timeout", {}, started_at, tool_calls, background_task_ids, context_truncated, error=str(exc)
        )
    except Exception as exc:
        logger.exception("Sub-agent %s failed", sub.name)
        return _sub_agent_result(
            sub, "error", {}, started_at, tool_calls, background_task_ids, context_truncated, error=str(exc)
        )

    return _sub_agent_result(sub, "empty", {}, started_at, tool_calls, background_task_ids, context_truncated)


def _background_task_ids(result: Any) -> list[str]:
    if not isinstance(result, dict) or result.get("status") != "background":
        return []
    task_id = str(result.get("task_id") or "").strip()
    return [task_id] if task_id else []


def _sub_agent_cancel_check(cancel_check: Callable[[], bool] | None, deadline: float) -> Callable[[], bool]:
    def _cancelled() -> bool:
        return bool(cancel_check and cancel_check()) or time.monotonic() >= deadline

    return _cancelled


def _fit_context(context: str, budget_tokens: int) -> tuple[str, bool]:
    text = str(context or "").strip()
    if not text:
        return "", False
    if estimate_tokens([{"role": "user", "content": text}]) <= budget_tokens:
        return text, False
    marker = "[上下文已按预算裁剪，仅保留最近部分]\n"
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = marker + text[-mid:] if mid else marker.strip()
        tokens = estimate_tokens([{"role": "user", "content": candidate}])
        if tokens <= budget_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best or marker.strip(), True


def _fit_result(text: str, budget_chars: int) -> tuple[str, bool]:
    raw = str(text or "")
    if len(raw) <= budget_chars:
        return raw, False
    marker = "\n\n[子 Agent 结果已按输出预算截断]"
    keep = max(0, budget_chars - len(marker))
    return raw[:keep].rstrip() + marker, True


def _sub_agent_result(
    sub: SubAgent,
    status: str,
    event: dict[str, Any],
    started_at: float,
    tool_calls: list[str],
    background_task_ids: list[str],
    context_truncated: bool,
    *,
    error: str = "",
) -> dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - started_at)
    result, result_truncated = _fit_result(
        event.get("text", "") if status == "completed" else "", sub.result_budget_chars
    )
    return {
        "agent": sub.name,
        "status": status,
        "result": result,
        "usage": event.get("usage", {}),
        "elapsed": elapsed,
        "rounds": event.get("rounds", 0),
        "tool_calls": tool_calls,
        "background_task_ids": list(dict.fromkeys(background_task_ids)),
        "context_truncated": context_truncated,
        "result_truncated": result_truncated,
        "error": error,
        "policy": _delegate_result_policy(sub, status),
    }


def _delegate_result_policy(sub: SubAgent, status: str) -> dict[str, Any]:
    next_action, retryable, instruction = _POLICY_BY_STATUS.get(
        status,
        ("fallback_to_direct_tools", False, "未知状态；用 fallback_tools 做保守降级。"),
    )
    fallback_tools = list(_FALLBACK_TOOLS_BY_AGENT.get(sub.name, ()))
    if next_action in {"use_result", "stop_and_report_cancelled"}:
        fallback_tools = []
    return {
        "next_action": next_action,
        "retryable": retryable,
        "fallback_tools": fallback_tools,
        "instruction": instruction,
    }


def _sub_agent_start_error(sub: SubAgent) -> dict[str, Any]:
    return _sub_agent_result(
        sub,
        "error",
        {},
        time.monotonic(),
        [],
        [],
        False,
        error="provider/registry 未注入，无法启动 sub-agent",
    )


# ---------------------------------------------------------------------------
# 委派工具函数 — 注册为 Orchestrator 可调用的工具
# ---------------------------------------------------------------------------


def delegate_to_research(task: str, context: str = "", *, tool_context=None) -> dict:
    """委派研究员收集数据。"""
    provider = getattr(tool_context, "provider", None)
    registry = getattr(tool_context, "registry", None)
    if not provider or not registry:
        return _sub_agent_start_error(RESEARCH_AGENT)
    on_progress = getattr(tool_context, "on_progress", None)
    cancel_check = getattr(tool_context, "cancel_check", None)
    return run_sub_agent(RESEARCH_AGENT, task, context, provider, registry, on_progress, cancel_check)


def delegate_to_analysis(task: str, context: str = "", *, tool_context=None) -> dict:
    """委派分析师做深度分析。"""
    provider = getattr(tool_context, "provider", None)
    registry = getattr(tool_context, "registry", None)
    if not provider or not registry:
        return _sub_agent_start_error(ANALYSIS_AGENT)
    on_progress = getattr(tool_context, "on_progress", None)
    cancel_check = getattr(tool_context, "cancel_check", None)
    return run_sub_agent(ANALYSIS_AGENT, task, context, provider, registry, on_progress, cancel_check)


def delegate_to_trading(task: str, context: str = "", *, tool_context=None) -> dict:
    """委派交易员做去留决策。"""
    provider = getattr(tool_context, "provider", None)
    registry = getattr(tool_context, "registry", None)
    if not provider or not registry:
        return _sub_agent_start_error(TRADING_AGENT)
    on_progress = getattr(tool_context, "on_progress", None)
    cancel_check = getattr(tool_context, "cancel_check", None)
    return run_sub_agent(TRADING_AGENT, task, context, provider, registry, on_progress, cancel_check)
