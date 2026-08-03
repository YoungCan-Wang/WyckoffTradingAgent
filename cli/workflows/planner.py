"""Model-authored workflow script planner."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from typing import Any

from cli.screen_intent import (
    stock_screen_candidate_request_hint,
    stock_screen_style_target_hint,
    stock_screen_suggested_args,
    stock_screen_temporal_buy_hint,
    stock_screen_theme_hint,
    stock_screen_watch_hint,
)
from cli.tools import TOOL_SCHEMAS, TOOL_SPECS
from cli.workflows._shared import (
    PORTFOLIO_REVIEW_CONTEXT_MARKERS,
    compact_text,
    has_stock_style_target,
    looks_like_portfolio_review,
    recent_dialogue_context,
)
from cli.workflows.models import WorkflowContext, WorkflowRun, WorkflowStep
from cli.workflows.router import route_workflow

MAX_WORKFLOW_STEPS = 24
ASK_USER_TOOL = "ask_user_question"
# planner 唯一的退出通道。没有它，planner 只能在「编不出计划」和「编一个替代任务」之间选后者：
# 昊华科技那次它自己写明了「当前工具集中没有可写入或更新持仓的工具」，然后照样拆成只读复核跑了 324 秒。
HANDOFF_FIELD = "handoff"
HANDOFF_DIRECT = "direct"
# 比 router 给得多：planner 要沿用上文里的代码、股数、成本价这类具体事实，
# 而 router 只需要判断本轮是不是承接。
_MAX_PLANNER_CONTEXT_MESSAGES = 8
_MAX_PLANNER_CONTEXT_CHARS = 600
TASK_LIST_FIELDS = ("tasks", "steps", "items", "subtasks", "jobs", "actions", "plan")
PHASE_LIST_FIELDS = ("phases", "stages", "stage_groups", "sections", "groups", "milestones")
SCRIPT_CONTAINER_FIELDS = ("workflow", "workflow_script", "script", "plan")
PROMPT_FIELDS = ("prompt", "instruction", "instructions", "task", "description", "goal", "objective")
TOOL_SCOPE_FIELDS = (
    "tool_scope",
    "allowed_tools",
    "required_tools",
    "required_tool",
    "tool_names",
    "tool_name",
    "tools",
    "tool",
    "functions",
    "function",
    "function_calls",
    "function_call",
    "tool_calls",
    "tool_call",
    "tool_uses",
    "tool_use",
    "calls",
    "call",
)
TOOL_ARG_FIELDS = ("args", "arguments", "tool_args", "tool_arguments", "parameters", "input", "inputs")
DEPENDENCY_FIELDS = ("depends_on", "dependsOn", "dependencies", "after", "needs", "requires")
TOOL_SCOPE_NESTED_FIELDS = (
    "tool_scope",
    "allowed",
    "required",
    "names",
    *TOOL_SCOPE_FIELDS,
)
_STOCK_FALLBACK_TARGETS = (
    "完整选股",
    "选股",
    "候选",
    "好股票",
    "好票",
    "好标的",
    "股票池",
    "机会",
    "etf",
    "基金",
    "行业基金",
    "值得复核",
    "值得跟踪",
)
_STOCK_FALLBACK_CONTEXT_MARKERS = ("a股", "股票", "股", "票", "标的", "市场", "板块", "行业", "方向")
_STOCK_FALLBACK_BUY_OPPORTUNITY_MARKERS = ("能买", "可买", "可以买", "买啥", "买什么", "值得买", "能不能买")
_STOCK_FALLBACK_TRACKING_MARKERS = ("值得关注", "值得看", "值得跟踪", "值得重点跟踪", "重点跟踪")
_STOCK_FALLBACK_EXPLAINERS = ("怎么", "如何", "方法", "是什么", "什么意思", "啥意思", "概念", "介绍", "解释", "说明")
_STOCK_FALLBACK_REPORT_MARKERS = ("研报", "深度", "报告")
_SYNTHESIS_TASK_MARKERS = (
    "汇总",
    "总结",
    "整合",
    "输出",
    "形成",
    "结论",
    "建议",
    "去留",
    "动作",
    "风险",
    "攻防",
    "复盘",
    "诊断",
)
_SYNTHESIS_CONTEXT_MARKERS = ("基于", "根据", "结合", "整合", "汇总", "上一", "前面", "已有")
_SYNTHESIS_TOOL_MARKERS = (
    ("portfolio", ("持仓", "仓位", "资金", "账户")),
    ("get_market_overview", ("市场", "大盘", "水温", "环境")),
    ("screen_stocks", ("候选", "选股", "股票池", "好股票", "好票", "标的")),
    ("generate_ai_report", ("研报", "报告", "深度", "复核")),
    ("generate_strategy_decision", ("攻防", "买卖", "计划", "触发", "失效", "动作", "边界", "风险")),
    ("analyze_stock", ("诊断", "结构", "个股", "股票")),
)
_TASK_SCREEN_INTENT_MARKERS = (
    "扫描",
    "筛选",
    "筛股",
    "选股",
    "股票池",
    "候选池",
    "机会池",
    "找好票",
    "找好标的",
    "好股票",
    "好票",
    "好标的",
)
_TASK_REPORT_INTENT_MARKERS = ("研报", "报告", "深度复核", "深度审讯")
_TASK_DECISION_INTENT_MARKERS = ("攻防", "触发", "失效", "买卖", "风险边界", "去留", "止损", "入场", "动作", "下一步")
_TASK_MARKET_INTENT_MARKERS = ("大盘", "市场", "水温", "盘面", "市场环境")
_TASK_PORTFOLIO_INTENT_MARKERS = ("持仓", "仓位", "资金", "账户", "组合")
_TASK_BACKTEST_INTENT_MARKERS = ("回测", "历史验证", "测算")
_TASK_ANALYZE_STOCK_TARGET_MARKERS = ("个股", "股票", "代码", "标的", "候选")
_PREVIOUS_DEPENDENCY_ALIASES = {
    "previous",
    "previous_step",
    "previous_task",
    "prior",
    "prior_step",
    "prior_task",
    "last",
    "last_step",
    "last_task",
    "上一步",
    "前一步",
    "上一个",
    "前一个",
    "上一任务",
    "前一任务",
    "上一阶段",
    "前一阶段",
    "前序任务",
}
ADAPTATION_HANDOFF_KEYS = (
    "last_strategy_decision",
    "last_ai_report",
    "last_stock_diagnosis",
    "last_recommendation_event_eval",
    "last_screen_result",
)

_PLAN_SYSTEM_PROMPT = """\
你是 Wyckoff CLI 的动态 workflow 编排器。

根据用户输入生成一个可执行 workflow script。script 是任务计划，不是解释文本，只能是 JSON。
自然语言语义、上下文恢复和任务拆分由你完成；runtime 只负责工具边界、并发、持久化和安全控制。

输出 JSON schema:
{
  "title": "简短中文标题",
  "rationale": "为什么这样拆分",
  "runtime": {
    "adaptive": true
  },
  "phases": [
    {
      "id": "phase_id",
      "title": "阶段标题",
      "tasks": [
        {
          "id": "task_id",
          "title": "任务标题",
          "tools": ["本 task 必须/允许使用的精确工具名；需要真实数据、分析或决策且工具摘要里有对应工具时必须填写"],
          "args": {"可选": "按工具 schema 给执行 agent 的参数提示，例如 board/limit 或 stock_codes"},
          "depends_on": ["可选，必须先完成的 task id"],
          "prompt": "完整任务说明",
          "context": "可选上下文",
          "rationale": "可选，为什么需要这一步",
          "success_criteria": "可选，这一步完成的判定标准",
          "risk_guard": "可选，这一步不能越过的边界"
        }
      ]
    }
  ],
  "synthesis_prompt": "最终汇总时应该如何整合结果"
}

如果这一轮根本不该由 workflow 承接，输出交还 JSON，不要硬编一份计划:
{"handoff":"direct","reason":"简短中文原因，说明为什么 workflow 办不成这件事"}

什么时候交还:
- 用户要的动作需要写入持仓、回填成交、改文件这类工具，而工具摘要里没有它们——编排拿不到这些工具，
  拆成「只读复核」并不是用户要的事。
- 单轮直接对话就能完成，不需要多阶段、并发或持久化计划。
- 请求指向的对象/动作还不明确，需要先问用户一句，而不是先跑一串任务。
交还比编一个替代任务好：交还只花几秒就回到直接对话，替代任务要跑几分钟、还交付了用户没要的东西。

运行边界:
- 只输出 JSON，不要 Markdown，不要代码块。
- phases 总任务数 1-24 个；如果要处理很多股票或对象，用工具批量处理，不要为每个对象单独生成 task。
- 同一 phase 内无依赖的 task 会并发执行；depends_on 指向的已命名 task 会先完成。
- 如果用 depends_on/after/needs/dependencies 表达 task 依赖，runtime 会跨 phase 按依赖顺序切批执行。
- 不需要选择内部执行角色；不要填写 agent/role。
- 工具是脚本契约：能用工具验证或交付的 task，必须用工具摘要里的精确工具名填写 tools；只有纯汇总/解释且不需要工具时才省略。
- 如果已能从用户请求或前序结果明确工具参数，可以在 task.args 写入；runtime 会作为参数提示交给执行 agent。
- 不要生成“识别代码/读取事实/形成计划”这类无工具占位 task；如果工具能完成，直接把对应工具写进同一个 task。
- 如果 task 需要候选、研报或持仓事实，填写 depends_on 指向提供这些事实的 task id，不要让它们无依赖并发。
- 选股交付如果要求候选、理由、风险边界或攻防计划，脚本里必须出现 screen_stocks，并在需要攻防/买卖计划/风险边界时出现 generate_strategy_decision；需要研报时再使用 generate_ai_report。
- 用户说“找几个/几只/一些候选”时，按多候选交付设计 task 和 synthesis_prompt，保留候选角色、排序、名称、理由、风险边界和下一步动作，不要只汇总成单一结论。
- 任务拆分围绕用户当前目标；能单步完成就生成 1 个 task，需要事实收集/分析/决策链路时再拆分。
- 如果后续任务是否执行、用什么工具、处理哪些候选明显依赖前序真实结果，在 runtime.adaptive 设为 true；runtime 会在 phase 之间让你基于真实结果重写剩余任务。
- 每个 task 尽量写清 rationale / success_criteria / risk_guard，让执行 agent 知道目标、验收和边界。
- 能用工具验证的事实交给 task 验证；只有执行对象仍不明确，或会产生写入、交易、高风险动作时才澄清。
- 能合理推断的表述偏差、口语省略、错别字或术语混用，按最高置信假设生成可执行 task，并让最终回答说明假设。
- 不要生成会写入持仓、交易或文件的任务。
"""

_REPAIR_SYSTEM_PROMPT = """\
你是 Wyckoff CLI workflow script 的结构修订器。

只输出修订后的完整 JSON，不要 Markdown，不要代码块。
你的任务不是执行工具，而是补齐 workflow script 的结构契约：
- 需要真实数据、分析、筛选、研报或策略决策的 task，必须从工具摘要中选择精确工具名写入 tools。
- 纯汇总、解释、最终整理的 task 可以继续省略 tools。
- 保留原有 task id、title、prompt、depends_on 和阶段结构；只有工具契约明显缺失时才改 tools。
- 不要新增写入、交易或文件修改类任务。
"""

_REVISION_SYSTEM_PROMPT = """\
你是 Wyckoff CLI workflow script 的动态改稿器。

用户正在批准前修改一个待执行 workflow script。你的任务是根据用户反馈重写完整 script JSON，不是执行工具。

改稿边界:
- 只输出完整 JSON，不要 Markdown，不要代码块。
- 保留用户原始目标，但优先服从最新反馈。
- 能删除、合并、重排或新增 task；不要只是给解释。
- 需要真实数据、分析、筛选、研报或策略决策的 task，必须从工具摘要中选择精确工具名写入 tools。
- 不要新增写入、交易或文件修改类任务。
- 每个 task 尽量写清 rationale / success_criteria / risk_guard。
"""

_ADAPTATION_SYSTEM_PROMPT = """\
你是 Wyckoff CLI workflow script 的运行中续写器。

workflow 已经完成一部分 task。你的任务是根据真实执行结果，重写“尚未执行的后续任务”，不是解释结果。

输出 JSON schema 与初始 workflow 相同，但只包含后续仍需要执行的 task：
{
  "title": "后续任务标题",
  "rationale": "为什么保留/新增/删除这些后续任务",
  "phases": [
    {
      "id": "phase_id",
      "title": "阶段标题",
      "tasks": [
        {
          "id": "task_id",
          "title": "任务标题",
          "tools": ["精确工具名"],
          "depends_on": ["可引用已完成 task id 或后续 task id"],
          "prompt": "完整任务说明",
          "rationale": "为什么需要这一步",
          "success_criteria": "完成判定",
          "risk_guard": "边界"
        }
      ]
    }
  ],
  "synthesis_prompt": "最终汇总应如何整合"
}

改稿边界:
- 只输出 JSON，不要 Markdown，不要代码块。
- 只写尚未执行的后续 task；不要重复已经完成的 task，除非真实结果显示必须重跑。
- 如果真实结果已经足够完成用户目标，输出 {"complete": true, "synthesis_prompt": "..."}。
- 可以删除、合并、重排或新增后续 task；以真实结果和用户目标为准。
- 需要真实数据、分析、筛选、研报或策略决策的 task，必须从工具摘要中选择精确工具名写入 tools。
- 如果 handoff 摘要里有 next_tool 或 diagnosis_targets，优先用对应工具和参数生成/保留下一步 task，例如 analyze_stock(code=..., mode=diagnose)。
- 保留候选护栏、数据质量、失效条件和风险边界；不要把受限候选写成买入建议。
- 不要新增写入、交易或文件修改类任务。
"""


def plan_workflow(
    user_text: str,
    *,
    session_id: str = "",
    context: WorkflowContext | None = None,
    provider: Any | None = None,
    tools: Any | None = None,
    workflow_script: dict[str, Any] | None = None,
    source_run_id: str = "",
    workflow_args: Any = None,
    only_step_id: str = "",
    messages: list[dict[str, Any]] | None = None,
) -> WorkflowRun:
    """Create a model-authored workflow run for one user turn."""

    context = context or route_workflow(user_text)
    raw_script = (
        _normalize_supplied_script(workflow_script, source_run_id, workflow_args, only_step_id)
        if workflow_script
        else _generate_script(user_text, context, provider, tools, messages)
    )
    if script_handoff_reason(raw_script):
        # 交还必须绕开 _script_steps：那里对空 phases 的处理是兜底生成一个 task，
        # 正好会把交还重新变成一份要跑的计划。
        return WorkflowRun(
            run_id=f"wf_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_text=user_text,
            context=context,
            steps=[],
            script=raw_script,
        )
    steps = _script_steps(raw_script, user_text, context)
    script = _script_with_step_tool_scopes(raw_script, steps)
    return WorkflowRun(
        run_id=f"wf_{uuid.uuid4().hex[:12]}",
        session_id=session_id,
        user_text=user_text,
        context=context,
        steps=steps,
        script=script,
    )


def workflow_steps_from_script(script: dict[str, Any], user_text: str, context: WorkflowContext) -> list[WorkflowStep]:
    """Build normalized workflow steps from a script without creating a new run."""

    return _script_steps(script, user_text, context)


def revise_workflow_script(
    user_text: str,
    feedback: str,
    current_script: dict[str, Any],
    *,
    context: WorkflowContext,
    provider: Any | None,
    tools: Any | None,
) -> dict[str, Any]:
    """Ask the model to rewrite a pending workflow script from chat feedback."""

    if provider is None:
        return _revision_failed_script(current_script, "provider unavailable")
    prompt = _revision_prompt(user_text, feedback, current_script, context, tools)
    try:
        text = _collect_planner_text(provider, prompt, _REVISION_SYSTEM_PROMPT)
        script = _normalize_generated_script(_loads_repair_script(text))
    except Exception as exc:
        return _revision_failed_script(current_script, f"revision failed: {exc}")
    if not isinstance(script, dict) or not _workflow_script_like(script):
        return _revision_failed_script(current_script, "revision returned non-workflow JSON")
    payload = _mark_revised_script(_limit_generated_script(script), feedback)
    return _repair_model_tool_contract(user_text, context, provider, tools, payload)


def adapt_workflow_script(
    user_text: str,
    current_script: dict[str, Any],
    completed_results: list[dict[str, Any]],
    remaining_steps: list[dict[str, Any]],
    *,
    context: WorkflowContext,
    provider: Any | None,
    tools: Any | None,
) -> dict[str, Any]:
    """Ask the model to rewrite the remaining workflow after phase results."""

    if provider is None:
        return _adaptation_failed_script(current_script, "provider unavailable")
    prompt = _adaptation_prompt(user_text, current_script, completed_results, remaining_steps, context, tools)
    try:
        text = _collect_planner_text(provider, prompt, _ADAPTATION_SYSTEM_PROMPT)
        script = _normalize_generated_script(_loads_repair_script(text))
    except Exception as exc:
        return _adaptation_failed_script(current_script, f"adaptation failed: {exc}")
    if not isinstance(script, dict):
        return _adaptation_failed_script(current_script, "adaptation returned non-workflow JSON")
    if _adaptation_complete(script):
        return _mark_adapted_script(script, completed=True)
    if not _workflow_script_like(script):
        return _adaptation_failed_script(current_script, "adaptation returned no continuation tasks")
    payload = _mark_adapted_script(_limit_generated_script(script))
    return _repair_model_tool_contract(user_text, context, provider, tools, payload)


def _adaptation_prompt(
    user_text: str,
    current_script: dict[str, Any],
    completed_results: list[dict[str, Any]],
    remaining_steps: list[dict[str, Any]],
    context: WorkflowContext,
    tools: Any | None,
) -> str:
    return (
        f"用户原始请求:\n{user_text}\n\n"
        f"运行上下文: {context.label} ({context.name})\n"
        f"当前可用工具摘要:\n{_tool_catalog(tools, context)}\n\n"
        f"优先 handoff 摘要（用于决定后续是否保留、删除、改写或停止）:\n"
        f"{json.dumps(_adaptation_handoff_summary(completed_results), ensure_ascii=False, default=str)[:6000]}\n\n"
        f"已完成结果:\n{json.dumps(completed_results, ensure_ascii=False, default=str)[:10000]}\n\n"
        f"原计划中尚未执行的 task:\n{json.dumps(remaining_steps, ensure_ascii=False, default=str)[:6000]}\n\n"
        f"当前完整 workflow JSON:\n{json.dumps(current_script, ensure_ascii=False, default=str)[:8000]}\n\n"
        "请只输出后续 continuation workflow JSON。"
    )


def _adaptation_handoff_summary(completed_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in reversed(completed_results):
        if len(seen) >= len(ADAPTATION_HANDOFF_KEYS):
            break
        result = item.get("result") if isinstance(item, dict) else {}
        handoff = result.get("handoff_state") if isinstance(result, dict) else {}
        if not isinstance(handoff, dict):
            continue
        step = item.get("step") if isinstance(item.get("step"), dict) else {}
        for key in ADAPTATION_HANDOFF_KEYS:
            value = handoff.get(key)
            if key in seen or not isinstance(value, dict):
                continue
            compact = _compact_adaptation_handoff_stage(key, value)
            if compact:
                seen.add(key)
                summary.append(
                    _compact_dict(
                        {
                            "step_id": step.get("step_id"),
                            "title": step.get("title"),
                            "source": key,
                            **compact,
                        }
                    )
                )
    return summary


def _compact_adaptation_handoff_stage(source: str, value: dict[str, Any]) -> dict[str, Any]:
    payload = _pick_compact_fields(
        value,
        (
            "status",
            "ok",
            "reason",
            "summary",
            "decision_brief",
            "selection_brief",
            "latest",
            "diagnosis_targets",
            "next_action",
            "next_tool",
            "message",
            "data_quality",
            "quality_gate",
        ),
    )
    payload["action_plan"] = _pick_compact_fields(
        value.get("action_plan"),
        (
            "candidate_action",
            "new_buy_allowed",
            "ai_review_allowed",
            "trade_readiness",
            "reason",
            "next_step",
            "next_tool",
            "review_targets",
            "diagnosis_targets",
        ),
    )
    payload["candidate_guard_summary"] = _pick_compact_fields(
        value.get("candidate_guard_summary"), ("direct_buy_blocked_count", "message")
    )
    payload["candidates"] = _adaptation_candidate_rows(source, value)
    return _compact_dict(payload)


def _adaptation_candidate_rows(source: str, value: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if source == "last_screen_result":
        selection = value.get("selection_brief") if isinstance(value.get("selection_brief"), dict) else {}
        rows.extend(_plain_list(value.get("report_candidates")))
        rows.extend(_plain_list(value.get("symbols_for_report")))
        rows.append(selection.get("primary_pick"))
        rows.extend(_plain_list(selection.get("best_candidates")))
        rows.extend(_plain_list(value.get("watch_candidates")))
        rows.extend(_plain_list(value.get("top_candidates")))
    elif source == "last_recommendation_event_eval":
        selection = value.get("policy_selection") if isinstance(value.get("policy_selection"), dict) else {}
        rows.extend(_plain_list(selection.get("picks")))
    elif source == "last_stock_diagnosis":
        rows.extend(_plain_list(value.get("diagnosed_symbols")))
        rows.append(value.get("latest"))
    else:
        rows.extend(_plain_list(value.get("reviewed_symbols")))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    ready_rank = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = _adaptation_candidate_identity(row)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        if str(row.get("action_status") or "").strip() == "ready_for_ai_review":
            ready_rank += 1
        candidates.append(_compact_adaptation_candidate(row, ready_rank))
        if len(candidates) >= limit:
            break
    return candidates


def _adaptation_candidate_identity(row: dict[str, Any]) -> str:
    for field in ("code", "symbol", "name"):
        value = str(row.get(field) or "").strip().lower()
        if value:
            return value
    return ""


def _compact_adaptation_candidate(row: dict[str, Any], ready_rank: int = 0) -> dict[str, Any]:
    payload = _pick_compact_fields(
        row,
        (
            "code",
            "name",
            "action_status",
            "status",
            "health",
            "status_label",
            "headline",
            "candidate_score",
            "latest_close",
            "latest_date",
            "trade_readiness",
            "new_buy_allowed",
            "stage",
            "candidate_lane",
            "candidate_shadow_score",
            "candidate_quality_score",
            "risk_adjusted_quality_score",
            "rank_reason",
            "quality_factors",
            "risk_factors",
            "entry_zone",
            "entry_zone_min",
            "entry_zone_max",
            "entry_trigger",
            "trigger_condition",
            "trigger_price",
            "trigger_level",
            "stop_loss",
            "effective_stop_loss",
            "invalidate_condition",
            "invalid_condition",
            "invalid_price",
            "invalid_level",
            "max_entry_price",
            "next_step",
            "data_status",
        ),
    )
    return _compact_dict({"candidate_role": _adaptation_candidate_role(row, ready_rank), **payload})


def _adaptation_candidate_role(row: dict[str, Any], ready_rank: int = 0) -> str:
    explicit = str(row.get("candidate_role") or row.get("role") or "").strip()
    if explicit:
        return explicit
    status = str(row.get("action_status") or row.get("status") or "").strip()
    if status == "ready_for_ai_review":
        if not str(row.get("code") or "").strip():
            return "待确认候选"
        if _limited_review_candidate(row):
            return "受限复核候选"
        return "备选复核候选" if ready_rank > 1 else "首选"
    if status == "repair_review_only":
        return "修复复核候选"
    if status == "confirmation_required":
        return "待确认候选"
    if status == "watch_only" or str(row.get("candidate_lane") or "").strip() == "watch":
        return "观察候选"
    if status.startswith("blocked_") or status in {"blocked", "rejected"}:
        return "阻断候选"
    return ""


def _limited_review_candidate(row: dict[str, Any]) -> bool:
    if row.get("new_buy_allowed") is False:
        return True
    readiness = str(row.get("trade_readiness") or "").strip()
    return readiness in {"research_only", "review_only"}


def _pick_compact_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _compact_dict({field: _compact_prompt_value(value.get(field)) for field in fields})


def _compact_prompt_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, list):
        return [_compact_prompt_value(item) for item in value[:4]]
    if isinstance(value, dict):
        return _compact_dict({str(key): _compact_prompt_value(item) for key, item in list(value.items())[:8]})
    return value


def _compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _adaptation_complete(script: dict[str, Any]) -> bool:
    return bool(script.get("complete") or script.get("done") or script.get("stop"))


def _mark_adapted_script(script: dict[str, Any], *, completed: bool = False) -> dict[str, Any]:
    payload = _mark_model_script(script)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime["adaptation"] = "model_phase"
    if completed:
        runtime["adaptation_complete"] = True
    payload["runtime"] = runtime
    return payload


def _adaptation_failed_script(script: dict[str, Any], reason: str) -> dict[str, Any]:
    payload = deepcopy(script)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.update({"adaptation": "failed", "adaptation_error": _clip_runtime_text(reason)})
    payload["runtime"] = runtime
    return payload


def _revision_prompt(
    user_text: str,
    feedback: str,
    current_script: dict[str, Any],
    context: WorkflowContext,
    tools: Any | None,
) -> str:
    return (
        f"用户原始请求:\n{user_text}\n\n"
        f"用户最新反馈:\n{feedback}\n\n"
        f"运行上下文: {context.label} ({context.name})\n"
        f"当前可用工具摘要:\n{_tool_catalog(tools, context)}\n\n"
        "请基于最新反馈重写完整 workflow JSON。\n\n"
        f"当前 workflow JSON:\n{json.dumps(current_script, ensure_ascii=False, default=str)[:8000]}"
    )


def _mark_revised_script(script: dict[str, Any], feedback: str) -> dict[str, Any]:
    payload = _mark_model_script(script)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.update({"revision": "model_feedback", "revision_feedback": _clip_runtime_text(feedback)})
    payload["runtime"] = runtime
    return payload


def _revision_failed_script(script: dict[str, Any], reason: str) -> dict[str, Any]:
    payload = deepcopy(script)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.update({"revision": "failed", "revision_error": _clip_runtime_text(reason)})
    payload["runtime"] = runtime
    return payload


def _clip_runtime_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _normalize_supplied_script(
    script: dict[str, Any],
    source_run_id: str,
    workflow_args: Any,
    only_step_id: str,
) -> dict[str, Any]:
    payload = _unwrap_script_container(deepcopy(script))
    runtime = payload.setdefault("runtime", {})
    if source_run_id:
        runtime["rerun_of"] = source_run_id
    if workflow_args not in (None, ""):
        runtime["args"] = workflow_args
    if only_step_id:
        runtime["only_step_id"] = only_step_id
    runtime["planner"] = "stored_script"
    return payload


def _mark_model_script(script: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(script)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.setdefault("planner", "model_script")
    payload["runtime"] = runtime
    return payload


def _limit_generated_script(script: dict[str, Any]) -> dict[str, Any]:
    phases = _script_phases(script)
    total = sum(len(_phase_tasks(phase)) for phase in phases)
    if total <= MAX_WORKFLOW_STEPS:
        return script
    payload = {key: value for key, value in script.items() if key not in (*TASK_LIST_FIELDS, "phases")}
    payload["phases"] = _limited_phases(phases)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.update(
        {
            "step_limit": MAX_WORKFLOW_STEPS,
            "original_step_count": total,
            "truncated_step_count": total - MAX_WORKFLOW_STEPS,
        }
    )
    payload["runtime"] = runtime
    return payload


def _limited_phases(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = 0
    limited: list[dict[str, Any]] = []
    for phase in phases:
        if kept >= MAX_WORKFLOW_STEPS:
            break
        tasks = _phase_tasks(phase)
        keep = tasks[: MAX_WORKFLOW_STEPS - kept]
        if keep:
            limited.append(_phase_with_tasks(phase, keep))
            kept += len(keep)
    return limited


def _phase_with_tasks(phase: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {key: value for key, value in phase.items() if key not in TASK_LIST_FIELDS}
    payload["tasks"] = tasks
    return payload


def _generate_script(
    user_text: str,
    context: WorkflowContext,
    provider: Any | None,
    tools: Any | None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if provider is None:
        return _fallback_script(user_text, context, reason="provider unavailable")
    prompt = _planner_user_prompt(user_text, context, tools, messages)
    try:
        text = _collect_planner_text(provider, prompt)
        script = _normalize_generated_script(_loads_script(text))
    except Exception as exc:
        return _fallback_script(user_text, context, reason=f"planner failed: {exc}")
    if not isinstance(script, dict):
        return _fallback_script(user_text, context, reason="planner returned non-object JSON")
    if reason := script_handoff_reason(script):
        # 交还不走 repair/limit：那两步是给「有任务的计划」补工具契约的，会把交还改回一份计划。
        return _handoff_script(reason)
    script = _mark_model_script(_limit_generated_script(script))
    return _repair_model_tool_contract(user_text, context, provider, tools, script)


def _repair_model_tool_contract(
    user_text: str,
    context: WorkflowContext,
    provider: Any,
    tools: Any | None,
    script: dict[str, Any],
) -> dict[str, Any]:
    if not _repairable_tool_names(context):
        return script
    unscoped_count = _unscoped_step_count(script, user_text, context)
    if unscoped_count <= 0:
        return script
    scoped_count = _scoped_step_count(script, user_text, context)
    prompt = _tool_contract_repair_prompt(user_text, context, tools, script, unscoped_count)
    try:
        text = _collect_planner_text(provider, prompt, _REPAIR_SYSTEM_PROMPT)
        repaired = _normalize_generated_script(_loads_repair_script(text))
    except Exception:
        return script
    if not isinstance(repaired, dict) or not _workflow_script_like(repaired):
        return script
    payload = _mark_model_script(_limit_generated_script(repaired))
    if _scoped_step_count(payload, user_text, context) < scoped_count:
        return script
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    runtime.update(
        {
            "tool_contract_repair": "model",
            "unscoped_step_count_before_repair": unscoped_count,
            "scoped_step_count_before_repair": scoped_count,
        }
    )
    payload["runtime"] = runtime
    return payload


def _repairable_tool_names(context: WorkflowContext) -> set[str]:
    return {name for name in _context_tool_names(context) if name != ASK_USER_TOOL}


def _unscoped_step_count(script: dict[str, Any], user_text: str, context: WorkflowContext) -> int:
    return sum(1 for step in _script_steps(script, user_text, context, infer_tools=False) if not step.tool_scope)


def _scoped_step_count(script: dict[str, Any], user_text: str, context: WorkflowContext) -> int:
    return sum(1 for step in _script_steps(script, user_text, context, infer_tools=False) if step.tool_scope)


def _tool_contract_repair_prompt(
    user_text: str,
    context: WorkflowContext,
    tools: Any | None,
    script: dict[str, Any],
    unscoped_count: int,
) -> str:
    return (
        f"用户请求:\n{user_text}\n\n"
        f"运行上下文: {context.label} ({context.name})\n"
        f"当前可用工具摘要:\n{_tool_catalog(tools, context)}\n\n"
        f"检测到 {unscoped_count} 个 task 没有声明 tools。"
        "请修订下面的 workflow JSON：需要真实数据/分析/筛选/研报/策略决策的 task 补齐 tools；"
        "纯汇总或解释 task 可以继续不声明 tools。\n\n"
        f"workflow JSON:\n{json.dumps(script, ensure_ascii=False)}"
    )


def _planner_user_prompt(
    user_text: str,
    context: WorkflowContext,
    tools: Any | None,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    catalog = _tool_catalog(tools, context)
    return (
        f"用户请求:\n{user_text}\n\n"
        f"{_planner_dialogue_block(messages, user_text)}"
        f"运行上下文: {context.label} ({context.name})\n"
        f"路由原因: {context.route_reason or '-'}\n\n"
        f"当前可用工具摘要（供你决定任务边界，不要直接调用）:\n{catalog}\n\n"
        "请生成 workflow JSON。"
    )


def _planner_dialogue_block(messages: list[dict[str, Any]] | None, user_text: str) -> str:
    """Give the planner the same dialogue tail the router already sees.

    没有这段历史，「继续录入」这类承接性请求只能被规划成「先问用户要录入什么」——上一轮
    刚说过的代码、股数、成本价对 planner 完全不可见，于是它去问用户已经给过的信息。
    """
    context = recent_dialogue_context(
        messages,
        user_text,
        max_messages=_MAX_PLANNER_CONTEXT_MESSAGES,
        max_chars=_MAX_PLANNER_CONTEXT_CHARS,
    )
    if not context:
        return ""
    return f"最近对话（本轮可能承接上文；上文已给出的事实要直接沿用，不要再问用户一遍）:\n{context}\n\n"


def _tool_catalog(tools: Any | None, context: WorkflowContext) -> str:
    allowed = _planner_visible_tools(context.allowed_tools or tuple(TOOL_SPECS))
    schemas = _planner_tool_schemas(tools, allowed)
    if schemas:
        return "\n".join(_tool_catalog_line(schema) for schema in schemas[:24])
    names = sorted(allowed)[:24]
    return "\n".join(f"- {name}: {_tool_display_name(name)}" for name in names)


def _planner_tool_schemas(tools: Any | None, allowed: set[str]) -> list[dict[str, Any]]:
    try:
        schemas = (
            tools.schemas(allowed) if tools else [schema for schema in TOOL_SCHEMAS if schema.get("name") in allowed]
        )
    except Exception:
        return []
    return [schema for schema in schemas if isinstance(schema, dict) and schema.get("name")]


def _tool_catalog_line(schema: dict[str, Any]) -> str:
    name = str(schema.get("name") or "")
    label = _tool_display_name(name)
    desc = _clip_runtime_text(schema.get("description"), 120)
    args = _tool_schema_args(schema)
    parts = [f"- {name} ({label})"]
    if desc:
        parts.append(desc)
    if args:
        parts.append(f"args={args}")
    return ": ".join(parts[:2]) + (f"；{parts[2]}" if len(parts) > 2 else "")


def _tool_display_name(name: str) -> str:
    spec = TOOL_SPECS.get(name)
    return spec.display_name if spec else name


def _tool_schema_args(schema: dict[str, Any], limit: int = 5) -> str:
    params = schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}
    props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
    required = set(params.get("required") or [])
    names = [str(name) + ("*" if name in required else "?") for name in list(props)[:limit]]
    if len(props) > limit:
        names.append(f"+{len(props) - limit}")
    return ",".join(names)


def _planner_visible_tools(names: tuple[str, ...]) -> set[str]:
    return {name for name in names if name and not name.startswith("delegate_to_")}


def _collect_planner_text(provider: Any, prompt: str, system_prompt: str = _PLAN_SYSTEM_PROMPT) -> str:
    chunks: list[str] = []
    messages = [{"role": "user", "content": prompt}]
    for chunk in provider.chat_stream(messages, [], system_prompt):
        if chunk.get("type") == "text_delta":
            chunks.append(str(chunk.get("text", "")))
    return "".join(chunks).strip()


def _loads_script(text: str) -> Any:
    raw = _strip_json_fence(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        if script := _outline_script(raw):
            return script
        raise


def _loads_repair_script(text: str) -> Any:
    raw = _strip_json_fence(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def script_handoff_reason(script: Any) -> str:
    """Return the planner's handoff reason when it declined to orchestrate this turn."""

    if not isinstance(script, dict):
        return ""
    if str(script.get(HANDOFF_FIELD) or "").strip().lower() != HANDOFF_DIRECT:
        return ""
    reason = " ".join(str(script.get("reason") or script.get("rationale") or "").split())
    return reason or "planner 判断本轮不适合 workflow"


def _handoff_script(reason: str) -> dict[str, Any]:
    return {
        HANDOFF_FIELD: HANDOFF_DIRECT,
        "reason": reason,
        "title": "交还直接对话",
        "phases": [],
        "runtime": {"planner": "model_handoff"},
    }


def _normalize_generated_script(script: Any) -> Any:
    if isinstance(script, dict):
        return _unwrap_script_container(script)
    if isinstance(script, list):
        return _lightweight_script(_safe_task_list(script), "planner returned top-level task list") or script
    if isinstance(script, str):
        return _outline_script(script) or script
    return script


def _unwrap_script_container(script: dict[str, Any]) -> dict[str, Any]:
    for field in SCRIPT_CONTAINER_FIELDS:
        nested = script.get(field)
        if isinstance(nested, dict) and _workflow_script_like(nested):
            return _script_with_container_defaults(script, nested)
    return script


def _script_with_container_defaults(container: dict[str, Any], nested: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(nested)
    for field in ("title", "rationale", "synthesis_prompt", "runtime"):
        if field in container and payload.get(field) in (None, "", [], {}):
            payload[field] = deepcopy(container[field])
    return payload


def _workflow_script_like(value: dict[str, Any]) -> bool:
    return bool(_first_phase_list(value) or _first_task_list(value) or _generated_task_like(value))


def _outline_script(text: str) -> dict[str, Any] | None:
    tasks = _text_task_items(text)
    return _lightweight_script(tasks, "planner returned outline text")


def _lightweight_script(tasks: list[dict[str, Any]], reason: str) -> dict[str, Any] | None:
    if not tasks:
        return None
    return {
        "title": "动态任务",
        "rationale": reason,
        "phases": [{"id": "outline", "title": "任务清单", "tasks": tasks}],
        "synthesis_prompt": "基于任务结果给出简洁中文答复。",
    }


def _strip_json_fence(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _script_steps(
    script: dict[str, Any],
    user_text: str,
    context: WorkflowContext,
    *,
    infer_tools: bool = True,
) -> list[WorkflowStep]:
    steps: list[WorkflowStep] = []
    args_text = _runtime_args(script)
    allowed_tool_names = _context_tool_names(context) or None
    infer_tools = infer_tools and _semantic_tool_inference_enabled(script)
    for phase in _script_phases(script):
        steps.extend(_phase_steps(phase, user_text, args_text, allowed_tool_names, infer_tools=infer_tools))
        if len(steps) >= MAX_WORKFLOW_STEPS:
            break
    steps = _normalize_step_dependencies(steps)
    steps = _stabilize_tool_dependencies(steps)
    steps = _filter_runtime_steps(steps, script)
    if steps:
        return steps[:MAX_WORKFLOW_STEPS]
    if only_step_id := _runtime_only_step_id(script):
        _mark_missing_only_step(script, only_step_id)
        return []
    fallback = _fallback_script(user_text, context, reason="planner returned no valid tasks")
    return _script_steps(fallback, user_text, context, infer_tools=infer_tools)


def _script_with_step_tool_scopes(script: dict[str, Any], steps: list[WorkflowStep]) -> dict[str, Any]:
    if not steps:
        return script
    payload = deepcopy(script)
    steps_by_id = {step.step_id: step for step in steps}
    for step_id, task in _script_task_refs(payload):
        step = steps_by_id.get(step_id)
        if not step:
            continue
        if step.tool_scope_source in {"model_declared", "semantic_inference"}:
            _set_script_task_tool_scope(task, step.tool_scope, args_hint=step.args_hint)
        _set_script_task_dependencies(task, step.depends_on)
    return payload


def _set_script_task_tool_scope(task: dict[str, Any], scope: tuple[str, ...], *, args_hint: str = "") -> None:
    for field in TOOL_SCOPE_FIELDS:
        if field != "tools":
            task.pop(field, None)
    task["tools"] = list(scope)
    if "args" not in task and (args := _simple_args_hint_mapping(args_hint)):
        task["args"] = args


def _simple_args_hint_mapping(text: str) -> dict[str, str]:
    args: dict[str, str] = {}
    for part in re.split(r"[；;\n]+", str(text or "")):
        if match := re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.+?)\s*$", part):
            args[match.group(1)] = str(match.group(2)).strip().strip("\"'")
    return {key: value for key, value in args.items() if value}


def _set_script_task_dependencies(task: dict[str, Any], dependencies: tuple[str, ...]) -> None:
    for field in DEPENDENCY_FIELDS:
        if field != "depends_on":
            task.pop(field, None)
    if dependencies:
        task["depends_on"] = list(dependencies)
    else:
        task.pop("depends_on", None)


def _script_task_refs(script: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    phases = _container_dict_refs(script, PHASE_LIST_FIELDS, keyed=True)
    if phases:
        refs: list[tuple[str, dict[str, Any]]] = []
        for phase_id, phase in phases:
            refs.extend(
                _container_dict_refs(phase, TASK_LIST_FIELDS, keyed=True)
                or ([(phase_id, phase)] if _generated_task_like(phase) else [])
            )
        return refs
    tasks = _container_dict_refs(script, TASK_LIST_FIELDS, keyed=True)
    if tasks:
        return tasks
    return [(_script_ref_id(script), script)] if _generated_task_like(script) else []


def _container_dict_refs(
    payload: dict[str, Any],
    fields: tuple[str, ...],
    *,
    keyed: bool,
) -> list[tuple[str, dict[str, Any]]]:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, list):
            refs = [(_script_ref_id(item), item) for item in value if isinstance(item, dict)]
            if refs:
                return refs
        if isinstance(value, dict):
            refs = [
                (_script_ref_id(item, fallback=str(key), keyed=keyed), item)
                for key, item in value.items()
                if isinstance(item, dict)
            ]
            if refs:
                return refs
    return []


def _script_ref_id(payload: dict[str, Any], fallback: str = "", *, keyed: bool = False) -> str:
    if keyed:
        raw = payload.get("id") or fallback or payload.get("title") or payload.get("name")
    else:
        raw = payload.get("id") or payload.get("title") or payload.get("name") or fallback
    return _slug(raw)


def _semantic_tool_inference_enabled(script: dict[str, Any]) -> bool:
    runtime = script.get("runtime") if isinstance(script.get("runtime"), dict) else {}
    return str(runtime.get("planner") or "") == "model_script"


def _phase_steps(
    phase: dict[str, Any],
    user_text: str,
    args_text: str,
    allowed_tool_names: set[str] | None,
    *,
    infer_tools: bool,
) -> list[WorkflowStep]:
    phase_id = _slug(phase.get("id") or phase.get("title") or "phase")
    steps: list[WorkflowStep] = []
    for task in _phase_tasks(phase):
        step = _task_step(task, phase_id, user_text, args_text, allowed_tool_names, infer_tools=infer_tools)
        if step:
            steps.append(step)
    return steps


def _task_step(
    task: dict[str, Any],
    phase_id: str,
    user_text: str,
    args_text: str,
    allowed_tool_names: set[str] | None,
    *,
    infer_tools: bool,
) -> WorkflowStep | None:
    if not _generated_task_like(task):
        return None
    title = str(task.get("title") or task.get("name") or task.get("id") or "task").strip()
    prompt = _task_prompt(task, title, user_text)
    prompt = _render_runtime_args(prompt, args_text)
    context = _render_runtime_args(str(task.get("context") or "").strip(), args_text)
    tool_scope, tool_scope_source = _task_tool_scope_with_source(task, allowed_tool_names, infer_tools=infer_tools)
    args_hint = _task_args_text(task, user_text, tool_scope, infer_tools=infer_tools)
    step_id = _slug(task.get("id") or title)
    return WorkflowStep(
        step_id=step_id,
        title=title[:80],
        tools=(),
        agent="task",
        prompt=prompt,
        context=context,
        args_hint=args_hint,
        rationale=_task_meta(task, ("rationale", "reason", "why")),
        success_criteria=_task_meta(task, ("success_criteria", "done_when", "acceptance_criteria", "expected_output")),
        risk_guard=_task_meta(task, ("risk_guard", "guard", "guardrail", "guardrails", "boundary", "constraints")),
        phase=phase_id,
        depends_on=_task_dependencies(task),
        tool_scope=tool_scope,
        tool_scope_source=tool_scope_source,
        dynamic=True,
    )


def _script_phases(script: dict[str, Any]) -> list[dict[str, Any]]:
    phases = _first_phase_list(script)
    if phases:
        return phases
    tasks = _first_task_list(script)
    if tasks:
        return [{"id": "top_level", "title": script.get("title") or "动态任务", "tasks": tasks}]
    if _generated_task_like(script):
        return [{"id": "top_level", "title": script.get("title") or "动态任务", "tasks": [script]}]
    return []


def _first_phase_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for field in PHASE_LIST_FIELDS:
        phases = _safe_list(payload.get(field))
        if phases:
            return phases
    return []


def _phase_tasks(phase: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = _first_task_list(phase)
    if tasks:
        return tasks
    return [phase] if _generated_task_like(phase) else []


def _first_task_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for field in TASK_LIST_FIELDS:
        items = _safe_task_list(payload.get(field))
        if items:
            return items
    return []


def _task_tool_scope(
    task: dict[str, Any],
    allowed_tool_names: set[str] | None = None,
    *,
    infer_tools: bool = True,
) -> tuple[str, ...]:
    return _task_tool_scope_with_source(task, allowed_tool_names, infer_tools=infer_tools)[0]


def _task_tool_scope_with_source(
    task: dict[str, Any],
    allowed_tool_names: set[str] | None = None,
    *,
    infer_tools: bool = True,
) -> tuple[tuple[str, ...], str]:
    names: list[str] = []
    for field in TOOL_SCOPE_FIELDS:
        for item in _tool_scope_items(task.get(field)):
            if name := _tool_name(item):
                names.append(name)
    explicit = tuple(dict.fromkeys(names))
    if explicit:
        return _filter_tool_scope(explicit, allowed_tool_names), "model_declared"
    if not infer_tools:
        return (), ""
    inferred = _inferred_task_tool_scope(task, allowed_tool_names)
    return (_filter_tool_scope(inferred, allowed_tool_names), "semantic_inference") if inferred else ((), "")


def _inferred_task_tool_scope(task: dict[str, Any], allowed_tool_names: set[str] | None) -> tuple[str, ...]:
    text = _task_intent_text(task)
    if not text or _looks_like_tool_explainer(text):
        return ()
    names = [
        name
        for name in (
            "portfolio",
            "get_market_overview",
            "screen_stocks",
            "analyze_stock",
            "generate_ai_report",
            "generate_strategy_decision",
            "run_backtest",
        )
        if _tool_allowed_for_inference(name, allowed_tool_names) and _task_text_matches_tool_intent(name, text)
    ]
    return tuple(dict.fromkeys(names))


def _task_intent_text(task: dict[str, Any]) -> str:
    parts = [
        task.get("title"),
        task.get("name"),
        task.get("id"),
        *(task.get(field) for field in PROMPT_FIELDS),
        task.get("context"),
        task.get("rationale"),
        task.get("success_criteria"),
    ]
    return compact_text(" ".join(str(part or "") for part in parts))


def _looks_like_tool_explainer(text: str) -> bool:
    return any(marker in text for marker in _STOCK_FALLBACK_EXPLAINERS)


def _tool_allowed_for_inference(name: str, allowed_tool_names: set[str] | None) -> bool:
    return allowed_tool_names is None or name in allowed_tool_names


def _task_text_matches_tool_intent(name: str, text: str) -> bool:
    if name == "portfolio":
        return not _looks_like_synthesis_intent_text(text) and any(
            marker in text for marker in _TASK_PORTFOLIO_INTENT_MARKERS
        )
    if name == "get_market_overview":
        return not _looks_like_synthesis_intent_text(text) and any(
            marker in text for marker in _TASK_MARKET_INTENT_MARKERS
        )
    if name == "screen_stocks":
        return any(marker in text for marker in _TASK_SCREEN_INTENT_MARKERS)
    if name == "analyze_stock":
        return _task_text_matches_stock_analysis(text)
    if name == "generate_ai_report":
        return any(marker in text for marker in _TASK_REPORT_INTENT_MARKERS)
    if name == "generate_strategy_decision":
        return any(marker in text for marker in _TASK_DECISION_INTENT_MARKERS)
    if name == "run_backtest":
        return any(marker in text for marker in _TASK_BACKTEST_INTENT_MARKERS)
    return False


def _task_text_matches_stock_analysis(text: str) -> bool:
    if _task_text_has_stock_identifier(text):
        return True
    if "结构诊断" in text or "个股诊断" in text:
        return True
    return "诊断" in text and any(marker in text for marker in _TASK_ANALYZE_STOCK_TARGET_MARKERS)


def _looks_like_synthesis_intent_text(text: str) -> bool:
    return any(marker in text for marker in _SYNTHESIS_TASK_MARKERS) and any(
        marker in text for marker in _SYNTHESIS_CONTEXT_MARKERS
    )


def _task_text_has_stock_identifier(text: str) -> bool:
    return bool(
        re.search(r"\b\d{6}\b", text)
        or re.search(r"\b[A-Z]{1,6}\.(?:US|HK)\b", text, re.IGNORECASE)
        or re.search(r"\b\d{5}\.HK\b", text, re.IGNORECASE)
    )


def _context_tool_names(context: WorkflowContext) -> set[str]:
    return {name for name in context.allowed_tools if name and not name.startswith("delegate_to_")}


def _filter_tool_scope(scope: tuple[str, ...], allowed_tool_names: set[str] | None) -> tuple[str, ...]:
    scope = _drop_question_tool_when_fact_tool_present(scope)
    if allowed_tool_names is None:
        return _ordered_tool_scope(scope)
    filtered = tuple(name for name in scope if name in allowed_tool_names)
    return _ordered_tool_scope(filtered)


_TOOL_SCOPE_PREDECESSORS = {
    "analyze_stock": ("screen_stocks",),
    "generate_ai_report": ("screen_stocks",),
    "generate_strategy_decision": ("screen_stocks", "generate_ai_report", "portfolio", "get_market_overview"),
}


def _ordered_tool_scope(scope: tuple[str, ...]) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(scope))
    if len(names) < 2:
        return names
    remaining = list(names)
    ordered: list[str] = []
    while remaining:
        progressed = False
        for name in list(remaining):
            blockers = [
                item for item in _TOOL_SCOPE_PREDECESSORS.get(name, ()) if item in names and item not in ordered
            ]
            if blockers:
                continue
            ordered.append(name)
            remaining.remove(name)
            progressed = True
        if not progressed:
            ordered.extend(remaining)
            break
    return tuple(ordered)


def _drop_question_tool_when_fact_tool_present(scope: tuple[str, ...]) -> tuple[str, ...]:
    concrete = [name for name in scope if name and name != ASK_USER_TOOL]
    if not concrete:
        return scope
    return tuple(name for name in scope if name != ASK_USER_TOOL)


def _tool_name(raw: Any) -> str:
    if isinstance(raw, dict):
        raw = _tool_name_payload_value(raw)
    raw_text = str(raw or "")
    key = _normalize_tool_key(raw_text)
    key = _TOOL_NAME_ALIASES.get(key, key)
    if key.startswith("delegate_to_"):
        return ""
    if key in TOOL_SPECS:
        return key
    return _embedded_tool_name(raw_text)


def _tool_name_payload_value(payload: dict[str, Any]) -> Any:
    for field in ("function", "tool"):
        nested = payload.get(field)
        if isinstance(nested, dict):
            return _tool_name_payload_value(nested)
    return (
        payload.get("name")
        or payload.get("tool")
        or payload.get("id")
        or payload.get("display_name")
        or payload.get("label")
    )


def _embedded_tool_name(raw: str) -> str:
    text = raw.lower()
    for name, spec in TOOL_SPECS.items():
        if name in text or str(spec.display_name or "") in raw:
            return "" if name.startswith("delegate_to_") else name
    return ""


def _normalize_tool_key(raw: Any) -> str:
    key = re.sub(r"[\s/-]+", "_", str(raw or "").strip().lower()).strip("_")
    key = re.sub(r"(_?tool|工具)$", "", key).strip("_")
    return key


def _tool_name_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, spec in TOOL_SPECS.items():
        for value in (name, spec.display_name):
            key = _normalize_tool_key(value)
            if key:
                aliases[key] = name
    return aliases


_TOOL_NAME_ALIASES = _tool_name_aliases()


def _generated_task_like(task: dict[str, Any]) -> bool:
    fields = ("id", "title", "name", *PROMPT_FIELDS, *TOOL_SCOPE_FIELDS)
    return any(str(task.get(field) or "").strip() for field in fields)


def _task_prompt(task: dict[str, Any], title: str, user_text: str) -> str:
    for field in PROMPT_FIELDS:
        value = str(task.get(field) or "").strip()
        if value:
            return value
    return title or user_text


def _task_meta(task: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        if value := _task_meta_value(task.get(field)):
            return value
    return ""


def _task_meta_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple, set)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "；".join(f"{key}: {text}" for key, item in value.items() if (text := _task_meta_item_value(item)))
    return str(value).strip()


def _task_meta_item_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _task_args_text(
    task: dict[str, Any],
    user_text: str = "",
    tool_scope: tuple[str, ...] = (),
    *,
    infer_tools: bool = False,
) -> str:
    inferred = _inferred_task_args(user_text, tool_scope, infer_tools=infer_tools)
    for field in TOOL_ARG_FIELDS:
        raw = task.get(field)
        if isinstance(raw, dict):
            if value := _task_meta_value({**inferred, **_clean_args_mapping(raw)}):
                return value
        elif value := _task_meta_value(raw):
            return value
    return _task_meta_value(inferred)


def _clean_args_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if str(key).strip() and str(item).strip()}


def _inferred_task_args(user_text: str, tool_scope: tuple[str, ...], *, infer_tools: bool) -> dict[str, str]:
    if not infer_tools or "screen_stocks" not in tool_scope:
        return {}
    return _stock_scan_args(user_text)


def _task_dependencies(task: dict[str, Any]) -> tuple[str, ...]:
    deps: list[str] = []
    for field in DEPENDENCY_FIELDS:
        deps.extend(dep for item in _field_items(task.get(field)) if (dep := _dependency_id(item)))
    return tuple(dict.fromkeys(dep for dep in deps if dep))


def _normalize_step_dependencies(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    aliases = _dependency_aliases(steps)
    for index, step in enumerate(steps):
        deps: list[str] = []
        for dep in step.depends_on:
            resolved = _resolve_step_dependency(dep, aliases, steps, index)
            if resolved and resolved != step.step_id and resolved not in deps:
                deps.append(resolved)
        step.depends_on = tuple(deps)
    return steps


def _dependency_aliases(steps: list[WorkflowStep]) -> dict[str, str]:
    buckets: dict[str, set[str]] = {}
    for step in steps:
        for alias in _dependency_alias_keys(step):
            if alias:
                buckets.setdefault(alias, set()).add(step.step_id)
    return {alias: next(iter(ids)) for alias, ids in buckets.items() if len(ids) == 1}


def _dependency_alias_keys(step: WorkflowStep) -> tuple[str, ...]:
    aliases: list[str] = []
    for value in (step.step_id, _slug(step.title)):
        if value:
            aliases.extend([value, value.lower()])
    return tuple(dict.fromkeys(aliases))


def _resolve_step_dependency(dep: str, aliases: dict[str, str], steps: list[WorkflowStep], index: int) -> str:
    if dep in aliases:
        return aliases[dep]
    if (lower_dep := dep.lower()) in aliases:
        return aliases[lower_dep]
    if dep in _PREVIOUS_DEPENDENCY_ALIASES:
        return _previous_step_dependency(steps, index)
    if ordinal := _ordinal_step_dependency(dep, steps):
        return ordinal
    if tool_name := _tool_name(dep):
        return _tool_step_dependency(steps, index, tool_name)
    return dep


def _ordinal_step_dependency(dep: str, steps: list[WorkflowStep]) -> str:
    match = re.fullmatch(r"(?:step_|task_|step|task)?(\d+)", dep.lower())
    if not match:
        return ""
    index = int(match.group(1)) - 1
    if index < 0 or index >= len(steps):
        return ""
    return steps[index].step_id


def _previous_step_dependency(steps: list[WorkflowStep], index: int) -> str:
    if index <= 0:
        return ""
    return steps[index - 1].step_id


def _tool_step_dependency(steps: list[WorkflowStep], index: int, tool_name: str) -> str:
    step = steps[index]
    return _nearest_tool_step_id(step, steps, tool_name)


def _stabilize_tool_dependencies(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    for step in steps:
        step.depends_on = _stabilized_step_dependencies(step, steps)
    return steps


def _stabilized_step_dependencies(step: WorkflowStep, steps: list[WorkflowStep]) -> tuple[str, ...]:
    scope = set(step.tool_scope)
    deps = list(step.depends_on)
    if "generate_ai_report" in scope and "screen_stocks" not in scope:
        _append_dependency(deps, _nearest_tool_step_id(step, steps, "screen_stocks"))
    if "analyze_stock" in scope and "screen_stocks" not in scope and not _step_has_explicit_stock_target(step):
        _append_dependency(deps, _nearest_tool_step_id(step, steps, "screen_stocks"))
    if "generate_strategy_decision" in scope and not scope.intersection({"screen_stocks", "generate_ai_report"}):
        _append_dependency(
            deps,
            _nearest_tool_step_id(step, steps, "generate_ai_report")
            or _nearest_tool_step_id(step, steps, "screen_stocks"),
        )
    if "generate_strategy_decision" in scope and not scope.intersection({"portfolio", "get_market_overview"}):
        _append_dependency(
            deps,
            _nearest_tool_step_id(step, steps, "portfolio")
            or _nearest_tool_step_id(step, steps, "get_market_overview"),
        )
    if not scope and not deps and _looks_like_synthesis_step(step):
        deps.extend(_synthesis_fact_step_ids(step, steps))
    return tuple(deps)


def _step_has_explicit_stock_target(step: WorkflowStep) -> bool:
    text = " ".join(
        [
            step.args_hint,
            step.prompt,
            step.context,
            step.title,
        ]
    )
    return bool(
        re.search(r"\b\d{6}\b", text)
        or re.search(r"\b[A-Z]{1,6}\.(?:US|HK)\b", text, re.IGNORECASE)
        or re.search(r"\b\d{5}\.HK\b", text, re.IGNORECASE)
    )


def _looks_like_synthesis_step(step: WorkflowStep) -> bool:
    text = _synthesis_step_text(step)
    return any(marker in text for marker in _SYNTHESIS_TASK_MARKERS) and any(
        marker in text for marker in _SYNTHESIS_CONTEXT_MARKERS
    )


def _synthesis_step_text(step: WorkflowStep) -> str:
    return compact_text(
        " ".join(
            [
                step.title,
                step.prompt,
                step.context,
                step.rationale,
                step.success_criteria,
            ]
        )
    )


def _synthesis_fact_step_ids(step: WorkflowStep, steps: list[WorkflowStep]) -> list[str]:
    candidates = [
        candidate
        for candidate in steps
        if candidate is not step and _step_has_fact_tool(candidate) and step.step_id not in candidate.depends_on
    ]
    text = _synthesis_step_text(step)
    previous = [candidate for candidate in candidates if _step_before(candidate, step, steps)]
    following = [candidate for candidate in candidates if _step_before(step, candidate, steps)]
    return (
        _matching_synthesis_fact_step_ids(previous, text)
        or [candidate.step_id for candidate in previous]
        or _matching_synthesis_fact_step_ids(following, text)
        or [candidate.step_id for candidate in following]
    )


def _matching_synthesis_fact_step_ids(steps: list[WorkflowStep], text: str) -> list[str]:
    return [step.step_id for step in steps if _step_matches_synthesis_text(step, text)]


def _step_matches_synthesis_text(step: WorkflowStep, text: str) -> bool:
    return any(_tool_matches_synthesis_text(tool_name, text) for tool_name in step.tool_scope)


def _tool_matches_synthesis_text(tool_name: str, text: str) -> bool:
    markers = next((markers for name, markers in _SYNTHESIS_TOOL_MARKERS if name == tool_name), ())
    return any(marker in text for marker in markers)


def _step_has_fact_tool(step: WorkflowStep) -> bool:
    return any(name for name in step.tool_scope if name != ASK_USER_TOOL)


def _nearest_tool_step_id(step: WorkflowStep, steps: list[WorkflowStep], tool_name: str) -> str:
    rows = [candidate for candidate in steps if _same_phase(step, candidate) and candidate is not step]
    return _nearest_step_id(step, steps, rows, tool_name) or _nearest_step_id(
        step,
        steps,
        [candidate for candidate in steps if candidate is not step],
        tool_name,
    )


def _nearest_step_id(
    step: WorkflowStep,
    steps: list[WorkflowStep],
    rows: list[WorkflowStep],
    tool_name: str,
) -> str:
    previous = [
        candidate for candidate in rows if tool_name in candidate.tool_scope and _step_before(candidate, step, steps)
    ]
    following = [
        candidate for candidate in rows if tool_name in candidate.tool_scope and _step_before(step, candidate, steps)
    ]
    candidate = (previous[-1:] or following[:1] or [None])[0]
    return candidate.step_id if candidate else ""


def _same_phase(left: WorkflowStep, right: WorkflowStep) -> bool:
    return (left.phase or "") == (right.phase or "")


def _step_before(left: WorkflowStep, right: WorkflowStep, steps: list[WorkflowStep]) -> bool:
    return steps.index(left) < steps.index(right)


def _append_dependency(deps: list[str], dep: str) -> None:
    if dep and dep not in deps:
        deps.append(dep)


def _dependency_id(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("id")
            or value.get("task_id")
            or value.get("step_id")
            or value.get("title")
            or value.get("name")
            or _tool_name(value)
        )
    text = str(value or "").strip()
    return _slug(text) if text else ""


def _field_items(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [part for part in re.split(r"[,，、\n]+", value) if part.strip()]
    return [value]


def _plain_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _tool_scope_items(value: Any) -> list[Any]:
    items: list[Any] = []
    for item in _field_items(value):
        nested = _nested_tool_scope_items(item)
        items.extend(nested or [item])
    return items


def _nested_tool_scope_items(value: Any) -> list[Any]:
    if not isinstance(value, dict):
        return []
    items: list[Any] = []
    for field in TOOL_SCOPE_NESTED_FIELDS:
        items.extend(_field_items(value.get(field)))
    return items


def _safe_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [_keyed_payload(key, item) for key, item in value.items() if isinstance(item, dict)]
    return []


def _safe_task_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return _text_task_items(value)
    if isinstance(value, list):
        return [payload for index, item in enumerate(value, 1) if (payload := _task_payload(item, index))]
    if isinstance(value, dict):
        return [payload for key, item in value.items() if (payload := _keyed_task_payload(key, item))]
    return []


def _text_task_items(text: str) -> list[dict[str, Any]]:
    lines = [_strip_list_marker(line) for line in str(text or "").splitlines()]
    items = [line for line in lines if line]
    if not items and text.strip():
        items = [text.strip()]
    return [_string_task_payload(item, index) for index, item in enumerate(items, 1)]


def _task_payload(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if isinstance(item, str):
        return _string_task_payload(item, index)
    return {}


def _keyed_task_payload(key: Any, item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return _keyed_payload(key, item)
    key_text = str(key or "").strip()
    if isinstance(item, str):
        payload = _string_task_payload(item, key_text or 1)
        if key_text:
            payload["id"] = key_text
        return payload
    return {}


def _string_task_payload(text: str, key: Any) -> dict[str, Any]:
    title = _strip_list_marker(text)
    if not title:
        return {}
    return {"id": str(key), "title": title, "prompt": title}


def _strip_list_marker(text: str) -> str:
    return re.sub(r"^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*", "", text).strip()


def _keyed_payload(key: Any, item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    key_text = str(key or "").strip()
    if key_text:
        payload.setdefault("id", key_text)
        payload.setdefault("title", key_text)
    return payload


def _runtime_args(script: dict[str, Any]) -> str:
    runtime = script.get("runtime", {})
    if not isinstance(runtime, dict):
        return ""
    args = runtime.get("args", "")
    return json.dumps(args, ensure_ascii=False, default=str) if isinstance(args, (dict, list)) else str(args or "")


def _render_runtime_args(prompt: str, args_text: str) -> str:
    if not args_text:
        return prompt
    if "{args}" in prompt:
        return prompt.replace("{args}", args_text)
    return f"{prompt}\n\n本次运行输入:\n{args_text}"


def _filter_runtime_steps(steps: list[WorkflowStep], script: dict[str, Any]) -> list[WorkflowStep]:
    only_step_id = _runtime_only_step_id(script)
    if not only_step_id:
        return steps
    return [step for step in steps if step.step_id == only_step_id]


def _runtime_only_step_id(script: dict[str, Any]) -> str:
    runtime = script.get("runtime", {})
    if not isinstance(runtime, dict):
        return ""
    return str(runtime.get("only_step_id", "") or "")


def _mark_missing_only_step(script: dict[str, Any], only_step_id: str) -> None:
    runtime = script.setdefault("runtime", {})
    if isinstance(runtime, dict):
        runtime["only_step_missing"] = only_step_id


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff-]+", "_", str(value or "task")).strip("_")
    return text[:40] or "task"


def _fallback_script(user_text: str, context: WorkflowContext, *, reason: str) -> dict[str, Any]:
    if stock_script := _stock_selection_fallback_script(user_text, context, reason):
        return stock_script
    if portfolio_script := _portfolio_review_fallback_script(user_text, context, reason):
        return portfolio_script
    title = _fallback_task_title(user_text, context)
    return {
        "title": title,
        "rationale": reason,
        "runtime": {"planner": "fallback_script", "fallback_reason": reason},
        "phases": [
            {
                "id": "single_pass",
                "title": "动态单步执行",
                "tasks": [
                    {
                        "id": "agent_task",
                        "title": title,
                        "prompt": _fallback_task_prompt(user_text),
                    }
                ],
            }
        ],
        "synthesis_prompt": "基于任务结果给出简洁中文答复。",
    }


def _stock_selection_fallback_script(user_text: str, context: WorkflowContext, reason: str) -> dict[str, Any] | None:
    if context.name != "dynamic_task" or not _looks_like_stock_selection_delivery(user_text):
        return None
    tasks = [_stock_scan_task(user_text), _stock_diagnosis_task()]
    if _wants_ai_report(user_text):
        tasks.append(_stock_report_task(depends_on=tasks[-1]["id"]))
    tasks.append(_stock_decision_task(depends_on=tasks[-1]["id"]))
    return {
        "title": "选股候选复核",
        "rationale": reason,
        "runtime": {
            "planner": "fallback_script",
            "fallback_reason": reason,
            "fallback_kind": "stock_selection",
        },
        "phases": [{"id": "stock_selection", "title": "选股复核", "tasks": tasks}],
        "synthesis_prompt": (
            "基于真实工具结果给出中文答复：按工具给出的候选角色和排序列出首选、备选复核候选、"
            "受限复核候选、修复复核候选、待确认候选、观察候选或阻断候选，保留代码、名称、核心理由、"
            "质量评分/评级、风险因素和下一步。"
            "如果工具结果显示禁止直接买入或仅适合观察，不得写成买入建议。"
        ),
    }


def _looks_like_stock_selection_delivery(user_text: str) -> bool:
    text = compact_text(user_text)
    if not text or any(marker in text for marker in _STOCK_FALLBACK_EXPLAINERS):
        return False
    return (
        bool(stock_screen_theme_hint(text))
        or any(marker in text for marker in _STOCK_FALLBACK_TARGETS)
        or _has_stock_buy_opportunity_target(text)
        or _has_stock_tracking_target(text)
        or stock_screen_watch_hint(text)
        or stock_screen_candidate_request_hint(text)
        or stock_screen_style_target_hint(text)
        or has_stock_style_target(text)
    )


def _has_stock_buy_opportunity_target(text: str) -> bool:
    return stock_screen_temporal_buy_hint(text) or (
        any(marker in text for marker in _STOCK_FALLBACK_CONTEXT_MARKERS)
        and any(marker in text for marker in _STOCK_FALLBACK_BUY_OPPORTUNITY_MARKERS)
    )


def _has_stock_tracking_target(text: str) -> bool:
    return any(marker in text for marker in _STOCK_FALLBACK_CONTEXT_MARKERS) and any(
        marker in text for marker in _STOCK_FALLBACK_TRACKING_MARKERS
    )


def _stock_scan_task(user_text: str) -> dict[str, Any]:
    payload = {
        "id": "scan_candidates",
        "title": "扫描候选",
        "tools": ["screen_stocks"],
        "prompt": (
            "按用户当前目标筛选股票候选，保留候选代码、名称、入选理由、质量评分/评级、风险因素、"
            "候选角色、排序、候选护栏和下一步。不要直接给买入指令，也不要把多候选合并成单一结论。\n\n"
            f"用户原文：{user_text}"
        ),
        "rationale": "先用真实筛选工具收集候选和质量证据。",
        "success_criteria": "输出带角色和排序的候选列表、质量证据、风险因素和是否允许进入下一步复核。",
        "risk_guard": "只做研究候选，不写入交易或持仓。",
    }
    if args := _stock_scan_args(user_text, default_all_board=True):
        payload["args"] = args
    return payload


def _stock_scan_args(user_text: str, *, default_all_board: bool = False) -> dict[str, str]:
    args = stock_screen_suggested_args(user_text, include_default_board=False)
    if default_all_board and "board" not in args and _stock_scan_defaults_to_all_board(user_text):
        return {"board": "all", **args}
    return args


def _stock_scan_defaults_to_all_board(user_text: str) -> bool:
    text = compact_text(user_text)
    if any(marker in text for marker in ("etf", "基金", "行业基金")):
        return False
    return (
        stock_screen_temporal_buy_hint(text)
        or stock_screen_watch_hint(text)
        or stock_screen_candidate_request_hint(text)
        or stock_screen_style_target_hint(text)
        or bool(stock_screen_theme_hint(text))
        or _has_stock_tracking_target(text)
        or any(marker in text for marker in _STOCK_FALLBACK_TARGETS)
        or has_stock_style_target(text)
    )


def _stock_diagnosis_task() -> dict[str, Any]:
    return {
        "id": "diagnose_candidates",
        "title": "诊断重点候选结构",
        "tools": ["analyze_stock"],
        "depends_on": ["scan_candidates"],
        "prompt": (
            "基于上一阶段 screen_stocks 的 handoff，对重点候选逐个做个股结构诊断。"
            "延续候选角色和排序，优先使用 tool args hint 里的 targets，不要让缺少手写代码阻塞执行。"
        ),
        "rationale": "筛股结果需要经过个股结构诊断，才能更接近可复核的好股票。",
        "success_criteria": "按候选角色输出重点候选的阶段、供需、触发位、失效位和主要风险。",
        "risk_guard": "只做结构诊断，不输出直接买入或交易执行指令。",
    }


def _stock_report_task(depends_on: str = "scan_candidates") -> dict[str, Any]:
    return {
        "id": "ai_report",
        "title": "生成候选研报",
        "tools": ["generate_ai_report"],
        "depends_on": [depends_on],
        "prompt": "基于上一阶段候选生成 AI 研报，保留结构、逻辑破产条件、储备营地和起跳板证据。",
        "rationale": "用户要求研报或深度复核时，需要在候选之后补充结构化研究。",
        "success_criteria": "候选研报包含结构判断、失效条件和可继续复核的对象。",
        "risk_guard": "研报只作研究，不输出交易执行指令。",
    }


def _stock_decision_task(depends_on: str) -> dict[str, Any]:
    return {
        "id": "strategy_decision",
        "title": "形成攻防边界",
        "tools": ["generate_strategy_decision"],
        "depends_on": [depends_on],
        "prompt": (
            "基于候选和已有研报/筛选证据形成攻防边界，沿用候选角色和排序，"
            "输出触发位、失效位、观察/复核/禁止直接买入状态。"
        ),
        "rationale": "用户要求风险、攻防、买卖计划或下一步时，需要把候选转成可复核的动作边界。",
        "success_criteria": "按候选角色输出每个重点候选的行动状态、触发条件、失效条件和风险护栏。",
        "risk_guard": "遵守候选护栏；未成熟、观察池或市场阻断候选不得转成直接买入。",
    }


def _wants_ai_report(user_text: str) -> bool:
    text = compact_text(user_text)
    return any(marker in text for marker in _STOCK_FALLBACK_REPORT_MARKERS)


def _portfolio_review_fallback_script(user_text: str, context: WorkflowContext, reason: str) -> dict[str, Any] | None:
    if context.name != "dynamic_task" or not _looks_like_portfolio_review_delivery(user_text):
        return None
    tasks = _portfolio_review_tasks(user_text)
    return {
        "title": "组合复盘",
        "rationale": reason,
        "runtime": {
            "planner": "fallback_script",
            "fallback_reason": reason,
            "fallback_kind": "portfolio_review",
        },
        "phases": [{"id": "portfolio_review", "title": "组合复盘", "tasks": tasks}],
        "synthesis_prompt": (
            "基于真实工具结果给出中文答复：先给组合结论，再分持仓去留、市场风险、今天/明天动作。"
            "不得声称已经执行交易或改仓。"
        ),
    }


def _portfolio_review_tasks(user_text: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if _wants_portfolio_market_context(user_text):
        tasks.append(_portfolio_market_task(user_text))
    tasks.append(_portfolio_diagnose_task(user_text))
    tasks.append(_portfolio_decision_task(depends_on=[task["id"] for task in tasks]))
    return tasks


def _looks_like_portfolio_review_delivery(user_text: str) -> bool:
    return looks_like_portfolio_review(user_text)


def _wants_portfolio_market_context(user_text: str) -> bool:
    text = compact_text(user_text)
    return any(marker in text for marker in PORTFOLIO_REVIEW_CONTEXT_MARKERS[:5])


def _portfolio_market_task(user_text: str) -> dict[str, Any]:
    return {
        "id": "market_context",
        "title": "读取市场环境",
        "tools": ["get_market_overview"],
        "prompt": f"读取当前大盘水温、风险状态和市场环境，供持仓复盘使用。\n\n用户原文：{user_text}",
        "rationale": "组合复盘需要先确认当前市场风险背景。",
        "success_criteria": "输出市场状态、风险闸门和对持仓动作的影响。",
        "risk_guard": "只读取市场事实，不生成交易执行指令。",
    }


def _portfolio_diagnose_task(user_text: str) -> dict[str, Any]:
    return {
        "id": "portfolio_diagnosis",
        "title": "读取并诊断持仓",
        "tools": ["portfolio"],
        "args": {"mode": "diagnose"},
        "prompt": f"读取用户真实持仓并做结构诊断，保留每只持仓的风险、阶段和去留证据。\n\n用户原文：{user_text}",
        "rationale": "持仓复盘必须先读取真实持仓数据。",
        "success_criteria": "输出每只持仓的结构诊断、风险因素和候选动作。",
        "risk_guard": "只读持仓，不写入、不调仓。",
    }


def _portfolio_decision_task(depends_on: list[str]) -> dict[str, Any]:
    return {
        "id": "portfolio_action_plan",
        "title": "形成去留和风险动作",
        "tools": ["generate_strategy_decision"],
        "depends_on": depends_on,
        "prompt": "基于市场环境和持仓诊断形成组合攻防计划，输出 EXIT/TRIM/HOLD/观察、触发条件和失效边界。",
        "rationale": "用户要求复盘、总结或策略建议时，需要把事实转成可执行边界。",
        "success_criteria": "输出持仓去留、风险动作、今天/明天观察条件和禁止执行边界。",
        "risk_guard": "不直接执行交易，不声称已完成买卖或持仓更新。",
    }


def _fallback_task_prompt(user_text: str) -> str:
    return (
        "直接处理用户请求。按上下文理解自然语言语义，并用可用工具读取或验证事实；"
        "只有工具无法恢复关键参数或涉及写入、交易、高风险确认时，才向用户澄清。\n\n"
        f"用户原文：{user_text}"
    )


def _fallback_task_title(user_text: str, context: WorkflowContext) -> str:
    text = re.sub(r"\s+", " ", user_text).strip(" \n\t。.")
    if not text:
        return context.label or "处理当前请求"
    return text[:40]
