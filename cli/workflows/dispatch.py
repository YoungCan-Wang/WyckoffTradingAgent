"""Runtime selection for natural-language turns."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cli.runtime import AgentRuntime
from cli.scratchpad import AgentScratchpad
from cli.workflows.executor import WorkflowExecutor
from cli.workflows.model_router import route_workflow_with_model
from cli.workflows.models import WorkflowContext

_DIRECT_TOOL_ORDER = (
    "search_stock_by_name",
    "analyze_stock",
    "portfolio",
    "get_market_overview",
    "get_market_history",
    "screen_stocks",
    "generate_ai_report",
    "generate_strategy_decision",
    "query_history",
    "evaluate_recommendation_events",
    "update_portfolio",
    "run_backtest",
    "check_background_tasks",
    "query_news_intelligence",
    "ask_user_question",
    "delegate_to_research",
    "delegate_to_analysis",
    "delegate_to_trading",
    "read_file",
    "write_file",
    "web_fetch",
    "exec_command",
)


def build_turn_runtime(
    provider: Any,
    tools: Any,
    *,
    session_id: str,
    user_text: str,
    scratchpad: AgentScratchpad | None = None,
    cancel_check: Callable[[], bool] | None = None,
    stream_chunk_timeout: float | None = None,
    workflow_context: WorkflowContext | None = None,
    workflow_script: dict[str, Any] | None = None,
    workflow_source_run_id: str = "",
    workflow_args: Any = None,
    workflow_only_step_id: str = "",
    enforce_turn_expectations: bool | None = None,
    routing_messages: list[dict[str, Any]] | None = None,
) -> tuple[Any, WorkflowContext]:
    """Return direct runtime for general chat, workflow executor for task turns."""

    workflow = workflow_context or route_workflow_with_model(user_text, provider, routing_messages)
    if workflow.is_general and not workflow_script:
        kwargs: dict[str, Any] = {
            "scratchpad": scratchpad,
            "cancel_check": cancel_check,
            "allowed_tools": infer_direct_allowed_tools(user_text),
            "enforce_turn_expectations": _direct_turn_expectations_default(enforce_turn_expectations),
        }
        if stream_chunk_timeout is not None:
            kwargs["stream_chunk_timeout"] = stream_chunk_timeout
        return AgentRuntime(provider, tools, **kwargs), workflow
    return (
        WorkflowExecutor(
            provider,
            tools,
            session_id=session_id,
            user_text=user_text,
            scratchpad=scratchpad,
            cancel_check=cancel_check,
            stream_chunk_timeout=stream_chunk_timeout,
            workflow_context=workflow,
            workflow_script=workflow_script,
            source_run_id=workflow_source_run_id,
            workflow_args=workflow_args,
            only_step_id=workflow_only_step_id,
            planning_messages=routing_messages,
        ),
        workflow,
    )


def infer_direct_allowed_tools(user_text: str) -> tuple[str, ...]:
    """Expose bounded direct-chat tools without keyword-gating intent."""

    return _DIRECT_TOOL_ORDER


def _direct_turn_expectations_default(value: bool | None) -> bool:
    return True if value is None else bool(value)
