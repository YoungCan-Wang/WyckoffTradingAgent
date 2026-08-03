"""Model-assisted workflow routing for natural-language turns."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any

from cli.screen_intent import (
    stock_screen_candidate_request_hint,
    stock_screen_style_target_hint,
    stock_screen_temporal_buy_hint,
    stock_screen_theme_hint,
    stock_screen_watch_hint,
)
from cli.workflows._shared import (
    STOCK_STYLE_MARKERS,
    STOCK_STYLE_TARGETS,
    compact_text,
    decision_confidence,
    dialogue_message_text,
    has_stock_style_target,
    loads_json,
    parse_confidence,
    provider_chat_response,
    recent_dialogue_context,
)
from cli.workflows.models import WorkflowContext
from cli.workflows.router import WORKFLOWS, route_resume_workflow, route_workflow

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 120
_MAX_ROUTING_CONTEXT_MESSAGES = 6
_MAX_ROUTING_CONTEXT_CHARS = 240
_VALID_MODES = {"direct", "dynamic_workflow"}
_MODE_FIELDS = ("mode", "route", "runtime", "execution_mode", "execution", "answer_mode", "plan_mode")
_MODE_VALUE_FIELDS = ("mode", "name", "value", "route", "runtime", "execution_mode", "execution", "type", "kind")
_DECISION_CONTAINER_FIELDS = ("decision", "routing", "router", "result", "selection", "choice", "classification")
_WORKFLOW_FLAG_FIELDS = (
    "workflow",
    "use_workflow",
    "dynamic_workflow",
    "needs_workflow",
    "needs_plan",
    "use_plan",
    "requires_plan",
    "needs_steps",
    "multi_step",
    "multi_stage",
)
_STOCK_SELECTION_SCOPE_MARKERS = (
    "完整选股",
    "候选股",
    "候选",
    "好股票",
    "好票",
    "好标的",
    "股票池",
    "机会",
    "值得复核",
    "值得跟踪",
)
_STOCK_SELECTION_STYLE_MARKERS = STOCK_STYLE_MARKERS
_STOCK_SELECTION_STYLE_TARGETS = STOCK_STYLE_TARGETS
_STOCK_CONTEXT_MARKERS = ("a股", "股票", "股", "票", "标的", "市场", "板块", "行业", "方向")
_STOCK_SELECTION_DELIVERY_MARKERS = (
    "找",
    "挑",
    "筛",
    "选",
    "几只",
    "几个",
    "理由",
    "风险",
    "风险边界",
    "攻防",
    "研报",
    "复核",
    "买卖计划",
    "行动计划",
    "触发位",
    "失效位",
    "下一步",
)
_STOCK_BUY_OPPORTUNITY_MARKERS = ("能买", "可买", "可以买", "买啥", "买什么", "值得买", "能不能买")
_THEME_SELECTION_DELIVERY_MARKERS = (
    *_STOCK_SELECTION_SCOPE_MARKERS,
    *_STOCK_SELECTION_STYLE_MARKERS,
    *_STOCK_SELECTION_DELIVERY_MARKERS,
    "哪些",
    "有哪些",
    "有什么",
)
_SHORT_STOCK_SELECTION_RE = re.compile(
    r"(?:选出|挑出|筛出|找(?:几只|几个)?|给我找|帮我找).{0,10}(?:好股票|好票|好标的|值得复核的票|值得跟踪的票)"
)
_STOCK_SELECTION_METHOD_MARKERS = ("怎么", "如何", "方法", "是什么", "什么是", "是什么意思", "啥意思", "概念", "解释")
# 写入账户事实的工具；dynamic_task 白名单里刻意没有它们。
_ACCOUNT_WRITE_TOOLS = ("update_portfolio", "record_trade_fill")
_ACCOUNT_WRITE_MARKERS = (
    "录入",
    "登记",
    "记一下",
    "记下",
    "加进持仓",
    "加入持仓",
    "计入持仓",
    "写入持仓",
    "更新持仓",
    "改持仓",
    "改一下持仓",
    "建仓",
    "补仓",
    "加仓",
    "减仓",
    "清仓",
    "调仓",
    "买入了",
    "卖出了",
    "已买入",
    "已卖出",
    "成交回填",
    "回填成交",
    "删掉持仓",
    "移除持仓",
)
_ACCOUNT_WRITE_METHOD_MARKERS = ("怎么", "如何", "方法", "是什么", "什么是", "啥意思", "概念", "解释", "能不能")
# 进 workflow 的置信度门槛。低于它就落 direct：错判成 direct 只多花几秒，错判成 workflow 要几分钟。
_WORKFLOW_CONFIDENCE_FLOOR = 0.7
_LOCAL_WRITE_TOOLS = ("write_file", "exec_command")
_LOCAL_WRITE_MARKERS = (
    "写个文件",
    "写入文件",
    "改一下文件",
    "保存到文件",
    "存成文件",
    "跑一下命令",
    "执行命令",
    "跑个脚本",
    "执行脚本",
)
# 「这一轮需要什么能力」→「哪些工具提供它」。判据是能力集合的差，关键词只用来推断需要哪种能力；
# 缺哪个工具由目标车道的实时白名单算出来，不写死。
_CAPABILITY_GROUPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("账户写入", _ACCOUNT_WRITE_TOOLS, _ACCOUNT_WRITE_MARKERS, _ACCOUNT_WRITE_METHOD_MARKERS),
    ("本地写入/执行", _LOCAL_WRITE_TOOLS, _LOCAL_WRITE_MARKERS, _ACCOUNT_WRITE_METHOD_MARKERS),
)
_MODE_ALIASES = {
    "direct": {
        "answer",
        "chat",
        "general",
        "general_chat",
        "normal",
        "single",
        "single_step",
        "直接",
        "直接回答",
        "直接处理",
        "普通对话",
        "普通聊天",
        "直答",
    },
    "dynamic_workflow": {
        "agentic",
        "background",
        "dynamic",
        "dynamic workflow",
        "multi_stage",
        "multi_step",
        "parallel",
        "plan",
        "planned",
        "workflow",
        "work_flow",
        "多阶段",
        "拆分执行",
        "动态 workflow",
        "动态任务",
        "动态工作流",
        "工作流",
        "计划执行",
    },
}

_ROUTER_SYSTEM_PROMPT = """\
你是 Wyckoff CLI 的 runtime router。用户只会在 agent 内聊天。

只选择本轮执行模式，不改写、不解释、不确认用户请求。
默认用 direct；只有需要持久化计划、并发/后台执行、跨对象复核或多阶段交付时，才用 dynamic_workflow。
解释概念、单对象判断、或不需要可见进度的单轮回答用 direct。
需要候选池、事实收集、交叉复核、理由、风险边界、行动计划等链路化交付时，用 dynamic_workflow。
口语、省略、错别字和术语混用按语义判断，不要按关键词逐字匹配。
confidence 只表示把握度，不会覆盖 mode 判断。

只输出 JSON:
{"mode":"direct|dynamic_workflow","confidence":0.0,"reason":"简短中文原因"}
"""


def route_workflow_with_model(
    user_text: str,
    provider: Any | None,
    messages: list[dict[str, Any]] | None = None,
) -> WorkflowContext:
    """Use the model as the primary semantic router when it is available."""

    resumed = route_resume_workflow(user_text)
    if resumed:
        return resumed
    fallback_context = route_workflow(user_text)
    decision, fallback_reason = _model_decision(user_text, provider, messages)
    if decision:
        if guarded := _guarded_context_for_model_decision(user_text, decision):
            return guarded
        return _context_from_model_decision(decision)
    if _account_write_tools_missing_from_workflow(user_text):
        # 兜底路径也要挡：显式 workflow 标记和选股兜底都可能把写入请求送进没有写入工具的通道。
        return _account_write_fallback_context(user_text, fallback_reason)
    if not fallback_context.is_general:
        return _context_with_router_fallback(fallback_context, fallback_reason)
    if guarded := _stock_selection_fallback_context(user_text, fallback_reason):
        return guarded
    # 组合复盘不再兜底升级 workflow：路由不可用时降级到 direct agent 更快也更稳，
    # 单工具就能回答的持仓诊断没必要跑后台编排。
    return _context_with_router_fallback(fallback_context, fallback_reason)


def _context_from_model_decision(decision: dict[str, Any]) -> WorkflowContext:
    return replace(
        WORKFLOWS["dynamic_task"] if _should_use_workflow(decision) else WORKFLOWS["general_chat"],
        route_reason=_model_route_reason(decision),
        route_confidence=float(decision["confidence"]),
        route_matches=("model_router",),
    )


def _guarded_context_for_model_decision(user_text: str, decision: dict[str, Any]) -> WorkflowContext | None:
    if _should_use_workflow(decision):
        if downgrade := _account_write_downgrade_context(user_text, decision):
            return downgrade
        return _low_confidence_downgrade_context(decision)
    if _needs_stock_selection_workflow_fallback(user_text):
        return replace(
            WORKFLOWS["dynamic_task"],
            route_reason=f"核心选股请求需要动态 workflow；覆盖模型 direct 判断：{decision['reason']}",
            route_confidence=0.68,
            route_matches=("model_router_guard", "stock_selection_guard"),
        )
    # 组合复盘不再覆盖模型的 direct 判断：模型看得到上下文，比关键词更清楚该不该编排。
    return None


def _low_confidence_downgrade_context(decision: dict[str, Any]) -> WorkflowContext | None:
    """Require real confidence to enter workflow, because the two misroutes cost very differently.

    判错成 direct：模型多调一个工具，几秒。判错成 workflow：几分钟、后台执行、计划落库、
    往对话里插好几个轮次、还可能零产出。confidence 以前算完就落库，dispatch 和 executor
    一次都没读过——0.2 和 0.9 一样照跑。不确定的时候按便宜的那种错法走。
    """

    if not decision.get("confidence_reported"):
        return None
    confidence = float(decision.get("confidence") or 0.0)
    if confidence >= _WORKFLOW_CONFIDENCE_FLOOR:
        return None
    return replace(
        WORKFLOWS["general_chat"],
        route_reason=(
            f"模型 workflow 判断置信度 {confidence:.2f} 低于门槛 {_WORKFLOW_CONFIDENCE_FLOOR:.2f}，"
            f"按直接对话处理：{decision['reason']}"
        ),
        route_confidence=confidence,
        route_matches=("model_router_guard", "workflow_confidence_floor"),
    )


def _account_write_downgrade_context(user_text: str, decision: dict[str, Any]) -> WorkflowContext | None:
    """Downgrade to direct chat when the request needs a tool the workflow does not have.

    workflow 的工具白名单里没有 update_portfolio / record_trade_fill——写入类动作被刻意
    挡在编排之外。所以「把这两只录进持仓」被判成 workflow 时，它不是跑得慢，是根本干不成：
    最好的结果也只是核对完代码再让用户自己去录。这类请求必须留在 direct agent。
    """
    capability, missing = missing_workflow_capability(user_text)
    if not missing:
        return None
    return replace(
        WORKFLOWS["general_chat"],
        route_reason=(f"{capability}请求需要 {', '.join(missing)}，动态 workflow 无此工具；覆盖模型 workflow 判断"),
        route_confidence=0.7,
        route_matches=("model_router_guard", "account_write_guard"),
    )


def _account_write_fallback_context(user_text: str, fallback_reason: str) -> WorkflowContext:
    capability, missing_tools = missing_workflow_capability(user_text)
    missing = ", ".join(missing_tools)
    label = _fallback_reason_label(fallback_reason) if fallback_reason else "模型判断"
    return replace(
        WORKFLOWS["general_chat"],
        route_reason=f"{capability}请求需要 {missing}，动态 workflow 无此工具（{label}），直接 agent 处理",
        route_confidence=0.7,
        route_matches=("model_router_fallback", "account_write_guard"),
    )


def missing_workflow_capability(user_text: str) -> tuple[str, tuple[str, ...]]:
    """Return the capability this turn needs and the tools dynamic_task lacks for it.

    模式决策必须发生在能力校验之后。路由只看语义、不看工具，就会把「录入持仓」这类请求分进一条
    结构上就干不成的车道——白名单里没有写入工具，最好的结果也只是核对完让用户自己再来一遍。
    """

    text = _compact_user_text(user_text)
    if not text:
        return "", ()
    allowed = set(WORKFLOWS["dynamic_task"].allowed_tools)
    for label, tool_names, markers, method_markers in _CAPABILITY_GROUPS:
        if any(marker in text for marker in method_markers):
            continue
        if not any(marker in text for marker in markers):
            continue
        if missing := tuple(name for name in tool_names if name not in allowed):
            return label, missing
    return "", ()


def _account_write_tools_missing_from_workflow(user_text: str) -> tuple[str, ...]:
    return missing_workflow_capability(user_text)[1]


def _looks_like_account_write_request(user_text: str) -> bool:
    return bool(_account_write_tools_missing_from_workflow(user_text))


def _model_route_reason(decision: dict[str, Any]) -> str:
    if _should_use_workflow(decision):
        return f"模型判断需要动态 workflow：{decision['reason']}"
    return f"模型判断直接处理：{decision['reason']}"


def _model_decision(
    user_text: str,
    provider: Any | None,
    messages: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    if provider is None:
        return None, "provider_unavailable"
    prompt = _router_user_prompt(user_text, messages)
    request_messages = [{"role": "user", "content": prompt}]
    try:
        response = _router_response(provider, request_messages)
        if response is None:
            return None, "router_response_unavailable"
        decision = _parse_decision(response)
        if decision is None:
            return None, "invalid_router_decision"
        return decision, ""
    except Exception:
        # 路由失败会静默改变整轮的执行路径，必须留下可见记录。
        logger.warning("model workflow router failed, falling back to keyword routing", exc_info=True)
        return None, "router_error"


def _router_user_prompt(user_text: str, messages: list[dict[str, Any]] | None = None) -> str:
    context = _recent_dialogue_context(messages, user_text)
    context_block = f"\n\n最近对话（仅用于判断本轮是否承接上一轮，不要改写用户请求）:\n{context}" if context else ""
    return f"用户请求:\n{user_text}{context_block}\n\n请输出 routing JSON。"


def _recent_dialogue_context(messages: list[dict[str, Any]] | None, current_user_text: str) -> str:
    return recent_dialogue_context(
        messages,
        current_user_text,
        max_messages=_MAX_ROUTING_CONTEXT_MESSAGES,
        max_chars=_MAX_ROUTING_CONTEXT_CHARS,
    )


def _routing_message_text(message: dict[str, Any]) -> str:
    return dialogue_message_text(message)


def _context_with_router_fallback(context: WorkflowContext, fallback_reason: str) -> WorkflowContext:
    if not fallback_reason:
        return context
    reason = _fallback_route_reason(context, fallback_reason)
    matches = tuple(dict.fromkeys(("model_router_fallback", *context.route_matches)))
    return replace(context, route_reason=reason, route_matches=matches)


def _stock_selection_fallback_context(user_text: str, fallback_reason: str) -> WorkflowContext | None:
    if not fallback_reason or not _needs_stock_selection_workflow_fallback(user_text):
        return None
    label = _fallback_reason_label(fallback_reason)
    return replace(
        WORKFLOWS["dynamic_task"],
        route_reason=f"模型路由不可用（{label}），核心选股请求兜底进入动态 workflow",
        route_confidence=0.62,
        route_matches=("model_router_fallback", "stock_selection_guard"),
    )


def _needs_stock_selection_workflow_fallback(user_text: str) -> bool:
    text = _compact_user_text(user_text)
    if not text or any(marker in text for marker in _STOCK_SELECTION_METHOD_MARKERS):
        return False
    if _has_theme_stock_selection_target(text):
        return True
    if _SHORT_STOCK_SELECTION_RE.search(text):
        return True
    if _has_stock_buy_opportunity_target(text):
        return True
    if stock_screen_watch_hint(text):
        return True
    if stock_screen_candidate_request_hint(text):
        return True
    if stock_screen_style_target_hint(text):
        return True
    has_scope = any(marker in text for marker in _STOCK_SELECTION_SCOPE_MARKERS) or _has_stock_style_target(text)
    has_delivery = any(marker in text for marker in _STOCK_SELECTION_DELIVERY_MARKERS)
    has_context = any(marker in text for marker in _STOCK_CONTEXT_MARKERS)
    return has_scope and has_delivery and has_context


def _has_stock_buy_opportunity_target(text: str) -> bool:
    return stock_screen_temporal_buy_hint(text) or (
        any(marker in text for marker in _STOCK_CONTEXT_MARKERS)
        and any(marker in text for marker in _STOCK_BUY_OPPORTUNITY_MARKERS)
    )


def _has_stock_style_target(text: str) -> bool:
    return has_stock_style_target(text)


def _has_theme_stock_selection_target(text: str) -> bool:
    return bool(stock_screen_theme_hint(text)) and any(marker in text for marker in _THEME_SELECTION_DELIVERY_MARKERS)


def _compact_user_text(value: Any) -> str:
    return compact_text(value)


def _fallback_route_reason(context: WorkflowContext, fallback_reason: str) -> str:
    label = _fallback_reason_label(fallback_reason)
    if context.is_general:
        return f"模型路由不可用（{label}），直接 agent 处理"
    return f"模型路由不可用（{label}），沿用兜底路由：{context.route_reason}"


def _fallback_reason_label(reason: str) -> str:
    return {
        "provider_unavailable": "无 provider",
        "router_response_unavailable": "无路由响应",
        "invalid_router_decision": "路由 JSON 无效",
        "router_error": "调用异常",
    }.get(reason, "未知原因")


def _router_response(provider: Any, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    return provider_chat_response(
        provider, messages, _ROUTER_SYSTEM_PROMPT, stream_fallback_flag="use_chat_stream_for_routing"
    )


def _parse_decision(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict) or response.get("type") == "tool_calls":
        return None
    try:
        payload = loads_json(str(response.get("text") or ""), error_label="router decision")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    payload = _router_decision_payload(payload)
    mode = _decision_mode(payload)
    if not mode:
        return None
    confidence = decision_confidence(payload)
    return {
        "mode": mode,
        "confidence": confidence,
        # 缺字段时 decision_confidence 也返回 0.0，和模型明确报 0.0 分不开。
        # 置信度门槛只对「真的报了一个低值」生效，不能因为 provider 不输出这个字段就关掉 workflow。
        "confidence_reported": _decision_reports_confidence(payload),
        "reason": _clean_reason(payload.get("reason")),
    }


def _decision_reports_confidence(payload: dict[str, Any]) -> bool:
    return any(parse_confidence(payload.get(key)) is not None for key in ("confidence", "score", "probability", "prob"))


def _router_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if _payload_has_top_level_decision_value(payload):
        return payload
    if nested := _nested_router_decision_payload(payload):
        return nested
    if _payload_has_decision_value(payload):
        return payload
    return payload


def _nested_router_decision_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    for field in (*_DECISION_CONTAINER_FIELDS, *_MODE_FIELDS, *_WORKFLOW_FLAG_FIELDS):
        nested = payload.get(field)
        if isinstance(nested, dict) and _payload_has_decision_value(nested):
            merged = dict(payload)
            merged.update(nested)
            return merged
    return None


def _payload_has_top_level_decision_value(payload: dict[str, Any]) -> bool:
    if any(not isinstance(payload.get(field), dict) and _mode_value(payload.get(field)) for field in _MODE_FIELDS):
        return True
    return _workflow_flag(payload) is not None


def _payload_has_decision_value(payload: dict[str, Any]) -> bool:
    if any(_mode_value(payload.get(field)) for field in _MODE_FIELDS):
        return True
    return _workflow_flag(payload) is not None


def _clean_reason(value: Any) -> str:
    reason = re.sub(r"\s+", " ", str(value or "")).strip()
    return reason[:_MAX_REASON_CHARS] or "需要多阶段任务编排"


def _decision_mode(payload: dict[str, Any]) -> str:
    for field in _MODE_FIELDS:
        if mode := _mode_value(payload.get(field)):
            return mode
    workflow_flag = _workflow_flag(payload)
    if workflow_flag is not None:
        return "dynamic_workflow" if workflow_flag else "direct"
    return ""


def _mode_value(value: Any) -> str:
    if isinstance(value, dict):
        for field in _MODE_VALUE_FIELDS:
            if mode := _mode_value(value.get(field)):
                return mode
        return ""
    text = _normalize_mode_text(value)
    if text in _VALID_MODES:
        return text
    for mode, aliases in _MODE_ALIASES.items():
        if text in aliases:
            return mode
    return ""


def _normalize_mode_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return text.replace("-", "_")


def _workflow_flag(payload: dict[str, Any]) -> bool | None:
    for field in _WORKFLOW_FLAG_FIELDS:
        if field in payload:
            return _coerce_bool(payload.get(field))
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "需要", "是"}:
        return True
    if text in {"0", "false", "no", "n", "不需要", "否"}:
        return False
    return None


def _should_use_workflow(decision: dict[str, Any] | None) -> bool:
    if not decision:
        return False
    return decision["mode"] == "dynamic_workflow"
