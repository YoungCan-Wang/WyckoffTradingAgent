"""Workflow router for model-authored task scripts."""

from __future__ import annotations

from dataclasses import replace

from cli.workflows.models import WorkflowContext

ASK_TOOLS = ("ask_user_question",)

WORKFLOWS: dict[str, WorkflowContext] = {
    "portfolio_review": WorkflowContext(
        name="portfolio_review",
        label="持仓复盘",
        allowed_tools=(
            "portfolio",
            "analyze_stock",
            "get_market_overview",
            "query_history",
            "delegate_to_analysis",
            "delegate_to_trading",
            *ASK_TOOLS,
        ),
        system_hint="当前 workflow 是历史持仓复盘上下文。先用工具读取事实，再让模型决定最小 task 拆分。",
    ),
    "backtest": WorkflowContext(
        name="backtest",
        label="策略回测",
        allowed_tools=(
            "run_backtest",
            "check_background_tasks",
            "get_market_history",
            "delegate_to_research",
            *ASK_TOOLS,
        ),
        system_hint="当前 workflow 是历史策略回测上下文。优先用已有默认值和工具探测可推断参数。",
    ),
    "stock_screen": WorkflowContext(
        name="stock_screen",
        label="选股扫描",
        allowed_tools=(
            "screen_stocks",
            "scan_corporate_events",
            "generate_ai_report",
            "query_history",
            "evaluate_recommendation_events",
            "get_market_overview",
            "get_market_history",
            "delegate_to_research",
            "delegate_to_analysis",
            *ASK_TOOLS,
        ),
        system_hint="当前 workflow 是历史选股扫描上下文。优先让模型生成必要的数据收集和筛选 task。",
    ),
    "stock_diagnosis": WorkflowContext(
        name="stock_diagnosis",
        label="个股诊断",
        allowed_tools=(
            "search_stock_by_name",
            "analyze_stock",
            "get_market_overview",
            "get_market_history",
            "query_history",
            "delegate_to_analysis",
            *ASK_TOOLS,
        ),
        system_hint="当前 workflow 是历史个股诊断上下文。先识别股票，再围绕结构和风险生成 task。",
    ),
    "dynamic_task": WorkflowContext(
        name="dynamic_task",
        label="动态任务",
        allowed_tools=(
            "search_stock_by_name",
            "scan_corporate_events",
            "analyze_stock",
            "portfolio",
            "get_market_overview",
            "get_market_history",
            "query_history",
            "screen_stocks",
            "evaluate_recommendation_events",
            "run_backtest",
            "check_background_tasks",
            "generate_ai_report",
            "generate_strategy_decision",
            "delegate_to_research",
            "delegate_to_analysis",
            "delegate_to_trading",
            *ASK_TOOLS,
        ),
        system_hint=(
            "当前 workflow 是模型生成的动态任务。自然语言理解、上下文恢复和 task 拆分由模型完成；"
            "代码只限制工具、写入和高风险动作边界。"
        ),
    ),
    "general_chat": WorkflowContext(name="general_chat", label="自由对话"),
}


def route_workflow(user_text: str) -> WorkflowContext:
    """Select hard workflow boundaries; model routing owns task semantics."""

    resumed = route_resume_workflow(user_text)
    if resumed:
        return resumed
    text = user_text.lower()
    if matches := _explicit_dynamic_workflow_matches(text):
        return _with_route(WORKFLOWS["dynamic_task"], "用户显式要求动态 workflow", 0.96, matches)
    return _with_route(WORKFLOWS["general_chat"], "普通工具型对话交给直接 agent", 0.0, ())


def route_resume_workflow(user_text: str) -> WorkflowContext | None:
    resumed = _resume_workflow_context(user_text.lower())
    if not resumed:
        return None
    return _with_route(resumed, "用户明确要求继续已有 workflow", 0.95, ("继续 workflow",))


def build_workflow_system_prompt(workflow: WorkflowContext | None) -> str:
    """Build a concise system prompt suffix for a selected workflow."""

    if not workflow or workflow.is_general:
        return ""
    tools = ", ".join(workflow.allowed_tools)
    route_line = f"Route reason: {workflow.route_reason}\n" if workflow.route_reason else ""
    return (
        "\n\n<workflow-runtime>\n"
        f"Workflow: {workflow.label} ({workflow.name})\n"
        f"{route_line}"
        f"Allowed tools for this turn: {tools}\n"
        f"{workflow.system_hint}\n"
        "先用工具验证事实；能合理推断的表述偏差、口语省略、错别字或术语混用直接按假设执行。"
        "只有关键对象仍缺失或涉及写入/交易/高风险动作时才提问。\n"
        "</workflow-runtime>"
    )


def _explicit_dynamic_workflow_matches(text: str) -> tuple[str, ...]:
    markers = ("ultracode", "用 workflow", "使用 workflow", "以 workflow", "用动态 workflow", "动态 workflow 跑")
    return tuple(marker for marker in markers if marker in text)


def _with_route(
    workflow: WorkflowContext,
    reason: str,
    confidence: float,
    matches: tuple[str, ...],
) -> WorkflowContext:
    return replace(
        workflow,
        route_reason=reason,
        route_confidence=confidence,
        route_matches=matches,
    )


def _resume_workflow_context(text: str) -> WorkflowContext | None:
    if "继续 workflow" not in text and "continue workflow" not in text:
        return None
    for name in ("portfolio_review", "backtest", "stock_screen", "stock_diagnosis"):
        workflow = WORKFLOWS[name]
        if name in text or workflow.label in text:
            return workflow
    return WORKFLOWS["dynamic_task"]
