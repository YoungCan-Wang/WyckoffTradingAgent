"""Shared tool-result serialization and compact previews."""

from __future__ import annotations

import json
import math
from typing import Any

from core.candidate_actions import candidate_action_fields, candidate_action_label, candidate_action_role
from core.candidate_guards import candidate_guard_reason
from core.candidate_preference import (
    candidate_matches_preference as _candidate_matches_preference,
)
from core.candidate_preference import (
    has_style_preference as _has_style_preference,
)
from core.candidate_preference import (
    has_theme_preference as _has_theme_preference,
)
from core.candidate_preference import (
    missing_style_preference_labels as _candidate_missing_style_preference_labels,
)
from core.candidate_preference import (
    preference_match_status as _preference_match_status,
)
from core.candidate_preference import (
    style_preference_match_status as _screen_style_preference_match_status,
)
from core.candidate_preference import (
    style_preference_text as _style_preference_text,
)
from core.candidate_preference import (
    theme_preference_text as _theme_preference_text,
)
from core.strategy_policy_display import policy_execution_mode_label, policy_next_action_label
from utils.safe import drop_empty as _drop_empty_preview_fields
from utils.safe import finite_float as _score_float

PREVIEW_CHARS = 2_000


def serialize_tool_result(result: Any) -> str:
    """Serialize a tool result exactly once for message context."""

    return json.dumps(_json_safe(result), ensure_ascii=False, default=str, allow_nan=False)


def tool_result_preview(tool_name: str, result: Any, content: str = "") -> str:
    if tool_name == "dynamic_workflow" and isinstance(result, dict):
        preview = _dynamic_workflow_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    if isinstance(result, dict) and _is_recommendation_event_eval_result(tool_name, result):
        preview = _recommendation_event_eval_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    if isinstance(result, dict) and _is_screen_stocks_result(tool_name, result):
        preview = _screen_stocks_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    if tool_name == "portfolio" and isinstance(result, dict):
        preview = _portfolio_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    if tool_name == "analyze_stock" and isinstance(result, dict):
        preview = _analyze_stock_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    if tool_name == "generate_ai_report" and isinstance(result, dict):
        preview = _ai_report_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    if tool_name == "generate_strategy_decision" and isinstance(result, dict):
        preview = _strategy_decision_preview(result)
        if preview:
            return preview[:PREVIEW_CHARS]
    return content[:PREVIEW_CHARS]


def tool_result_brief_lines(tool_name: str, result: Any, *, max_lines: int = 3) -> list[str]:
    if not isinstance(result, dict) or (result.get("error") and tool_name != "dynamic_workflow"):
        return []
    lines: list[str] = []
    if tool_name == "dynamic_workflow":
        lines = _dynamic_workflow_brief_lines(result, max_lines=max_lines)
    elif _is_recommendation_event_eval_result(tool_name, result):
        lines = _recommendation_event_eval_brief_lines(result, max_lines=max_lines)
    elif _is_screen_stocks_result(tool_name, result):
        lines = _screen_stocks_brief_lines(result, max_lines=max_lines)
    elif tool_name == "portfolio":
        lines = _portfolio_brief_lines(result, max_lines=max_lines)
    elif tool_name == "analyze_stock":
        lines = _analyze_stock_brief_lines(result, max_lines=max_lines)
    elif tool_name == "generate_ai_report":
        lines = _ai_report_brief_lines(result, max_lines=max_lines)
    elif tool_name == "generate_strategy_decision":
        lines = _strategy_decision_brief_lines(result, max_lines=max_lines)
    return _compact_brief_lines(lines, max_lines)


def _compact_brief_lines(lines: list[str], max_lines: int) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = str(line or "").strip()
        if text:
            out.append(text)
        if len(out) >= max_lines:
            break
    return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    return value


def _is_recommendation_event_eval_result(tool_name: str, result: dict[str, Any]) -> bool:
    return (
        tool_name in {"recommendation_event_eval", "evaluate_recommendation_events"}
        or result.get("job_kind") == "recommendation_event_eval"
    )


def _is_screen_stocks_result(tool_name: str, result: dict[str, Any]) -> bool:
    return tool_name == "screen_stocks" or result.get("job_kind") == "funnel_screen"


def _recommendation_event_eval_preview(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    payload = _drop_empty_preview_fields(
        {
            "ok": result.get("ok"),
            "job_kind": result.get("job_kind"),
            "result_summary": _text_excerpt(result.get("result_summary"), 900),
            "candidate_conclusion": _candidate_conclusion_preview("last_recommendation_event_eval", result),
            "candidate_guard_summary": _candidate_guard_preview(result.get("candidate_guard_summary")),
            "metadata": _recommendation_metadata_preview(result.get("metadata")),
            "all": _recommendation_metric_preview(summary.get("all")),
            "ranking_decision": _recommendation_ranking_decision_preview(summary.get("ranking_decision")),
            "policy_selection": _recommendation_policy_selection_preview(result.get("policy_selection")),
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _recommendation_event_eval_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    lines = [line.strip() for line in str(result.get("result_summary") or "").splitlines() if line.strip()]
    if not lines:
        lines = _recommendation_fallback_brief_lines(result)
    conclusion_line = _candidate_conclusion_brief_line("last_recommendation_event_eval", result)
    guard_line = _candidate_guard_brief_line(result.get("candidate_guard_summary"))
    handoff_line = _recommendation_next_tool_brief_line(result.get("policy_selection"))
    pick_lines = [] if conclusion_line else _recommendation_policy_brief_lines(result.get("policy_selection"))
    reserve_handoff = bool(handoff_line) and max_lines > 3
    reserved = int(bool(conclusion_line)) + int(bool(guard_line)) + int(bool(pick_lines)) + int(reserve_handoff)
    lines = lines[: max(max_lines - reserved, 0)]
    if conclusion_line:
        lines.append(conclusion_line)
    if guard_line:
        lines.append(guard_line)
    if handoff_line:
        lines.append(handoff_line)
    if pick_lines:
        lines.extend(pick_lines[: max_lines - len(lines)])
    return lines[:max_lines]


def _recommendation_fallback_brief_lines(result: dict[str, Any]) -> list[str]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    all_rows = summary.get("all") if isinstance(summary.get("all"), dict) else {}
    decision = summary.get("ranking_decision") if isinstance(summary.get("ranking_decision"), dict) else {}
    ready = f"{all_rows.get('rows_ready', 0)}/{all_rows.get('rows_total', 0)}"
    lines = [
        f"推荐事件评估: ready={ready}, hit={_format_pct(all_rows.get('hit_rate_pct'))}%, "
        f"ranking_decision={decision.get('status', 'unknown')}",
        _recommendation_decision_line(decision),
    ]
    if pick_line := _recommendation_policy_line(result.get("policy_selection")):
        lines.append(pick_line)
    return [line for line in lines if line]


def _recommendation_metadata_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = ("market", "horizon_days", "target_pct", "max_dates", "records", "codes")
    return _drop_empty_preview_fields({key: value.get(key) for key in keys})


def _recommendation_metric_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = (
        "rows_ready",
        "rows_total",
        "hit_rate_pct",
        "close_win_rate_pct",
        "avg_close_return_horizon_pct",
        "avg_mfe_horizon_pct",
        "avg_mae_horizon_pct",
    )
    return _drop_empty_preview_fields({key: value.get(key) for key in keys})


def _recommendation_ranking_decision_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "status": value.get("status"),
            "recommended_strategy": value.get("recommended_strategy"),
            "recommended_top_k": value.get("recommended_top_k"),
            "watch_strategy": value.get("watch_strategy"),
            "reason": value.get("reason"),
            "candidates": _recommendation_decision_candidates_preview(value.get("candidates")),
        }
    )


def _recommendation_decision_candidates_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(strategy): _drop_empty_preview_fields(
            {
                "status": row.get("status"),
                "top_k": row.get("top_k"),
                "decision_score": row.get("decision_score"),
                "rows_ready": row.get("rows_ready"),
                "hit_rate_delta_pct": row.get("hit_rate_delta_pct"),
                "avg_mfe_delta_pct": row.get("avg_mfe_delta_pct"),
                "avg_mae_delta_pct": row.get("avg_mae_delta_pct"),
                "sample_ok": row.get("sample_ok"),
                "lift_ok": row.get("lift_ok"),
                "risk_ok": row.get("risk_ok"),
            }
        )
        for strategy, row in value.items()
        if isinstance(row, dict)
    }


def _recommendation_policy_selection_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "status": value.get("status"),
            "selection_strategy": value.get("selection_strategy"),
            "top_k": value.get("top_k"),
            "recommend_date": value.get("recommend_date"),
            "uses_promoted_ranking": value.get("uses_promoted_ranking"),
            "watch_strategy": value.get("watch_strategy"),
            "reason": value.get("reason"),
            "action_plan": _recommendation_action_plan_preview(value.get("action_plan")),
            "picks": _recommendation_pick_preview_list(value.get("picks"), 6),
        }
    )


def _recommendation_action_plan_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "primary_action": value.get("primary_action"),
            "candidate_action": value.get("candidate_action"),
            "new_buy_allowed": value.get("new_buy_allowed"),
            "ai_review_allowed": value.get("ai_review_allowed"),
            "trade_readiness": value.get("trade_readiness"),
            "review_status": value.get("review_status"),
            "reason": value.get("reason"),
            "next_step": value.get("next_step"),
            "next_tool": value.get("next_tool"),
        }
    )


def _recommendation_next_tool_brief_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    action_plan = value.get("action_plan") if isinstance(value.get("action_plan"), dict) else {}
    next_tool = action_plan.get("next_tool") if isinstance(action_plan.get("next_tool"), dict) else {}
    if not next_tool:
        return ""
    payload = dict(next_tool)
    payload.setdefault("reason", action_plan.get("reason") or action_plan.get("next_step"))
    return _next_tool_brief_line(payload)


def _recommendation_pick_preview_list(value: Any, limit: int) -> list[dict[str, Any]]:
    rows = _preview_list(value, limit)
    return [_recommendation_pick_preview(row) for row in rows if isinstance(row, dict)]


def _recommendation_pick_preview(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "rank",
        "selection_strategy",
        "code",
        "name",
        "recommend_date",
        "is_ai_recommended",
        "funnel_score",
        "recommend_count",
        "candidate_shadow_score",
        "candidate_shadow_grade",
        "entry_quality_score",
        "entry_quality_grade",
        "entry_quality_risk_flags",
        "candidate_quality_score",
        "risk_adjusted_quality_score",
        "entry_risk_penalty",
        "label_ready",
        "label_status",
        "action_status",
        "action_label",
        "action_level",
        "direct_buy_allowed",
        "quality_factors",
        "risk_factors",
        "next_step",
    )
    return _drop_empty_preview_fields({key: value.get(key) for key in keys})


def _recommendation_decision_line(decision: dict[str, Any]) -> str:
    status = str(decision.get("status") or "unknown")
    strategy = str(decision.get("recommended_strategy") or decision.get("watch_strategy") or "score_only")
    top_k = decision.get("recommended_top_k") or "n/a"
    if status == "candidate":
        return f"排序接入候选: {strategy} top{top_k} 已通过样本/lift/风险门槛"
    if status == "watch":
        return f"排序观察项: {strategy} 有改善但未全部过门槛"
    return "排序策略: 继续保持 score_only"


def _recommendation_policy_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    picks = value.get("picks") if isinstance(value.get("picks"), list) else []
    names = [_candidate_name(pick) for pick in picks if isinstance(pick, dict)]
    names = [name for name in names if name]
    if not names:
        return ""
    strategy = str(value.get("selection_strategy") or "score_only")
    rec_date = value.get("recommend_date") or "-"
    return f"最新候选({rec_date}, {strategy}): {', '.join(names[:5])}"


def _recommendation_policy_brief_lines(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    picks = value.get("picks") if isinstance(value.get("picks"), list) else []
    lines = [_candidate_brief_line(pick) for pick in picks if isinstance(pick, dict)]
    return [line for line in lines if line]


def _format_pct(raw: Any) -> str:
    try:
        return f"{float(raw):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "n/a"


def _dynamic_workflow_preview(result: dict[str, Any]) -> str:
    payload = _drop_empty_preview_fields(
        {
            "workflow_run_id": result.get("workflow_run_id"),
            "workflow": result.get("workflow"),
            "status": "failed" if result.get("error") else "completed",
            "error": _text_excerpt(result.get("error"), 500),
            "candidate_conclusions": _dynamic_workflow_candidate_conclusions(result),
            "final_text": _text_excerpt(result.get("final_text"), 1400),
            "elapsed": result.get("elapsed"),
            "events": _dynamic_workflow_event_preview(result.get("events")),
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _dynamic_workflow_candidate_conclusions(result: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for item in _preview_list(result.get("candidate_conclusions"), 5):
        line = item.get("line") if isinstance(item, dict) else item
        _append_preview_line(rows, line, limit=5)
    for line in str(result.get("final_text") or "").splitlines():
        if "候选结论:" in line:
            _append_preview_line(rows, line, limit=5)
    return rows


def _dynamic_workflow_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    lines = [_dynamic_workflow_status_line(result)]
    if error_line := _dynamic_workflow_error_line(result.get("error")):
        lines.append(error_line)
    for line in _dynamic_workflow_handoff_lines(result, limit=max_lines):
        lines.append(line)
        if len(lines) >= max_lines:
            return lines
    if step_line := _dynamic_workflow_last_step_line(result.get("events")):
        lines.append(step_line)
    if final_line := _dynamic_workflow_final_line(result, existing=lines):
        lines.append(final_line)
    return lines[:max_lines]


def _dynamic_workflow_status_line(result: dict[str, Any]) -> str:
    status = "失败" if result.get("error") else "完成"
    parts = [
        f"动态 workflow: {status}",
        _text_excerpt(result.get("workflow"), 60),
        _text_excerpt(result.get("workflow_run_id"), 40),
        _dynamic_workflow_elapsed_text(result.get("elapsed")),
    ]
    return " · ".join(part for part in parts if part)


def _dynamic_workflow_error_line(value: Any) -> str:
    text = _text_excerpt(value, 180)
    return f"错误: {text}" if text else ""


def _dynamic_workflow_elapsed_text(value: Any) -> str:
    try:
        elapsed = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{elapsed:.1f}s" if math.isfinite(elapsed) else ""


def _dynamic_workflow_handoff_lines(result: dict[str, Any], *, limit: int) -> list[str]:
    rows = list(_dynamic_workflow_candidate_conclusions(result))
    for event in _preview_list(result.get("events"), 20):
        step = event.get("step") if isinstance(event, dict) else {}
        if not isinstance(step, dict):
            continue
        for evidence in _preview_list(step.get("evidence"), 6):
            text = str(evidence or "")
            if "候选结论:" in text or "候选护栏:" in text:
                if _dynamic_workflow_handoff_seen(rows, text):
                    continue
                _append_preview_line(rows, text, limit=limit)
    return rows[:limit]


def _dynamic_workflow_handoff_seen(rows: list[str], value: str) -> bool:
    key = _dynamic_workflow_handoff_key(value)
    return bool(key and any(_dynamic_workflow_handoff_key(row) == key for row in rows))


def _dynamic_workflow_handoff_key(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("候选结论:"):
        return text.split(" · ", 1)[0].strip()
    return text


def _dynamic_workflow_last_step_line(value: Any) -> str:
    for event in reversed(_preview_list(value, 20)):
        step = event.get("step") if isinstance(event, dict) else {}
        if not isinstance(step, dict):
            continue
        title = _text_excerpt(step.get("title"), 80)
        summary = _text_excerpt(step.get("summary"), 120)
        status = _text_excerpt(step.get("status"), 40)
        detail = " · ".join(part for part in (title, status, summary) if part)
        if detail:
            return f"最近步骤: {detail}"
    return ""


def _dynamic_workflow_final_line(result: dict[str, Any], *, existing: list[str]) -> str:
    for line in str(result.get("final_text") or "").splitlines():
        text = _text_excerpt(line, 180)
        if text and text not in existing:
            return text
    return ""


def _append_preview_line(rows: list[str], value: Any, *, limit: int) -> None:
    if len(rows) >= limit:
        return
    text = _text_excerpt(str(value or "").strip(" \t-•"), 240)
    if text and text not in rows:
        rows.append(text)


def _dynamic_workflow_event_preview(value: Any) -> list[dict[str, Any]]:
    rows = _preview_list(value, 8)
    return [
        _drop_empty_preview_fields(
            {
                "type": row.get("type"),
                "workflow": row.get("workflow"),
                "phase": row.get("phase"),
                "status": row.get("status"),
                "step": _dynamic_workflow_step_preview(row.get("step")),
            }
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _dynamic_workflow_step_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "title": _text_excerpt(value.get("title"), 120),
            "status": value.get("status"),
            "summary": _text_excerpt(value.get("summary"), 240),
            "evidence": [_text_excerpt(item, 240) for item in _preview_list(value.get("evidence"), 4)],
        }
    )


def _portfolio_preview(result: dict[str, Any]) -> str:
    payload = _drop_empty_preview_fields(
        {
            "portfolio_id": result.get("portfolio_id"),
            "message": result.get("message"),
            "free_cash": result.get("free_cash"),
            "total_market_value": result.get("total_market_value"),
            "total_assets": result.get("total_assets"),
            "total_equity": result.get("total_equity"),
            "position_count": result.get("position_count"),
            "successful_count": result.get("successful_count"),
            "failed_count": result.get("failed_count"),
            "market_value_note": result.get("market_value_note"),
            "positions": _portfolio_position_preview(result.get("positions"), 8),
            "diagnostics": _portfolio_diagnostic_preview(result.get("diagnostics"), 8),
            "tickflow_limit_hint": result.get("tickflow_limit_hint"),
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _portfolio_position_preview(value: Any, limit: int) -> list[dict[str, Any]]:
    return [
        _drop_empty_preview_fields(
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "shares": row.get("shares"),
                "cost_price": row.get("cost_price") or row.get("cost"),
                "buy_dt": row.get("buy_dt"),
            }
        )
        for row in _preview_list(value, limit)
        if isinstance(row, dict)
    ]


def _portfolio_diagnostic_preview(value: Any, limit: int) -> list[dict[str, Any]]:
    return [
        _drop_empty_preview_fields(
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "shares": row.get("shares"),
                "cost": row.get("cost"),
                "market_value": row.get("market_value"),
                "weight_pct": row.get("weight_pct"),
                "health": row.get("health"),
                "pnl_pct": row.get("pnl_pct"),
                "pnl_amount": row.get("pnl_amount"),
                "latest_close": row.get("latest_close"),
                "latest_date": row.get("latest_date"),
                "l2_channel": row.get("l2_channel"),
                "l4_triggers": _preview_list(row.get("l4_triggers"), 4),
                "candidate_score": row.get("candidate_score"),
                "health_reasons": _preview_list(row.get("health_reasons"), 4),
                "diagnosis_brief": _diagnosis_brief_preview(row.get("diagnosis_brief")),
                "error": row.get("error"),
            }
        )
        for row in _preview_list(value, limit)
        if isinstance(row, dict)
    ]


def _portfolio_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    if isinstance(result.get("diagnostics"), list):
        return _portfolio_diagnosis_brief_lines(result, max_lines=max_lines)
    return _portfolio_view_brief_lines(result, max_lines=max_lines)


def _portfolio_view_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    positions = _portfolio_position_preview(result.get("positions"), 5)
    count = _portfolio_position_count(result, positions)
    lines = [_portfolio_view_headline(result, count)]
    if positions:
        lines.append(f"持仓明细: {'；'.join(_portfolio_position_line(row) for row in positions[:3])}")
    elif result.get("message"):
        lines.append(_text_excerpt(result.get("message"), 120))
    lines.append(_portfolio_empty_next_step(count))
    return [line for line in lines if line][:max_lines]


def _portfolio_diagnosis_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    diagnostics = [row for row in _portfolio_diagnostic_preview(result.get("diagnostics"), 8) if row]
    count = _portfolio_position_count(result, diagnostics)
    lines = [_portfolio_diagnosis_headline(result, count)]
    for row in _rank_portfolio_diagnostics(diagnostics):
        if line := _portfolio_diagnostic_line(row):
            lines.append(line)
        if len(lines) >= max_lines:
            break
    if count == 0:
        lines.append(_portfolio_empty_next_step(count))
    return [line for line in lines if line][:max_lines]


def _portfolio_position_count(result: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    try:
        return int(result.get("position_count") or len(rows))
    except (TypeError, ValueError):
        return len(rows)


def _portfolio_view_headline(result: dict[str, Any], count: int) -> str:
    cash = _format_money(result.get("free_cash"))
    if count <= 0:
        return f"持仓: 暂无头寸 · 现金{cash}"
    return f"持仓: {count}只 · 现金{cash}"


def _portfolio_diagnosis_headline(result: dict[str, Any], count: int) -> str:
    cash = _format_money(result.get("free_cash"))
    success = _safe_int_text(result.get("successful_count"))
    failed = _safe_int_text(result.get("failed_count"))
    status = f"成功{success or '-'}，失败{failed or '0'}"
    parts = [f"持仓诊断: {count}只", status]
    # 总市值/总资产是"该减谁"的前提，headline 不带的话模型只能按结构风险排序
    if market_value := _format_positive_money(result.get("total_market_value")):
        parts.append(f"总市值{market_value}")
    parts.append(f"现金{cash}")
    if total_assets := _format_positive_money(result.get("total_assets")):
        parts.append(f"总资产{total_assets}")
    return " · ".join(parts)


def _portfolio_position_line(row: dict[str, Any]) -> str:
    name = _candidate_name(row)
    shares = _safe_int_text(row.get("shares"))
    cost = _format_score(row.get("cost_price"))
    parts = [f"{shares}股" if shares else "", f"成本{cost}" if cost else ""]
    detail = " ".join(part for part in parts if part)
    return f"{name} {detail}".strip()


def _format_positive_money(value: Any) -> str:
    """只在金额可用时给出文本，0/缺失都返回空串，避免 headline 里出现"总市值0.00"。"""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(amount) or amount <= 0:
        return ""
    return f"{amount:,.2f}"


def _portfolio_empty_next_step(count: int) -> str:
    if count > 0:
        return ""
    return "下一步: 直接在聊天里发持仓代码 / 成本 / 仓位，我会继续做诊断"


def _rank_portfolio_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_portfolio_diagnostic_rank)


def _portfolio_diagnostic_rank(row: dict[str, Any]) -> tuple[int, float, str]:
    """先按健康度分档，同档内重仓优先——brief 只留 3 行时，被截掉的应该是小仓位。"""
    health = str(row.get("health") or "")
    if row.get("error"):
        tier = 0
    elif "危险" in health:
        tier = 1
    elif "警戒" in health:
        tier = 2
    else:
        tier = 3
    return (tier, -_portfolio_row_weight(row), _candidate_name(row))


def _portfolio_row_weight(row: dict[str, Any]) -> float:
    for key in ("weight_pct", "market_value"):
        try:
            value = float(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


def _portfolio_diagnostic_line(row: dict[str, Any]) -> str:
    brief = row.get("diagnosis_brief") if isinstance(row.get("diagnosis_brief"), dict) else {}
    parts = [
        _stock_value_part("", row.get("health")),
        # 仓位占比排在现价前面：决定"先动谁"看的是权重，不是价格
        _stock_value_part("仓位", _format_weight_pct(row.get("weight_pct"))),
        _stock_value_part("市值", _format_positive_money(row.get("market_value"))),
        _stock_value_part("现价", row.get("latest_close")),
        _stock_value_part("盈亏", _format_pct_signed(row.get("pnl_pct"))),
        _stock_value_part("通道", row.get("l2_channel")),
        _stock_list_part("风险", _portfolio_risk_items(row, brief), 2),
        _stock_next_step_part(brief.get("next_step")),
        _stock_value_part("错误", row.get("error")),
    ]
    detail = " · ".join(part for part in parts if part)
    return f"{_candidate_name(row)} · {detail}" if detail else _candidate_name(row)


def _portfolio_risk_items(row: dict[str, Any], brief: dict[str, Any]) -> list[Any]:
    risks = brief.get("risks")
    if isinstance(risks, list):
        return risks
    return [item for item in _preview_list(row.get("health_reasons"), 4) if not _positive_diagnostic_reason(item)]


def _positive_diagnostic_reason(value: Any) -> bool:
    text = str(value or "").strip()
    return text in {"多头排列", "MA50>MA200(偏强)"} or text.startswith(("L2通道:", "L4信号:"))


def _format_money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount:,.2f}"


def _format_weight_pct(value: Any) -> str:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(pct) or pct <= 0:
        return ""
    return f"{pct:.1f}%"


def _format_pct_signed(value: Any) -> str:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(pct):
        return ""
    return f"{pct:+.2f}%"


def _analyze_stock_preview(result: dict[str, Any]) -> str:
    payload = _drop_empty_preview_fields(
        {
            "code": result.get("code"),
            "name": result.get("name"),
            "latest_date": result.get("latest_date"),
            "latest_close": result.get("latest_close"),
            "data_status": result.get("data_status"),
            "diagnosis_brief": _diagnosis_brief_preview(result.get("diagnosis_brief")),
            "health": result.get("health"),
            "ma_pattern": result.get("ma_pattern"),
            "l2_channel": result.get("l2_channel"),
            "track": result.get("track"),
            "l4_triggers": _preview_list(result.get("l4_triggers"), 6),
            "candidate_lane": result.get("candidate_lane"),
            "candidate_entry_type": result.get("candidate_entry_type"),
            "candidate_score": result.get("candidate_score"),
            "exit_signal": result.get("exit_signal"),
            "health_reasons": _preview_list(result.get("health_reasons"), 6),
            "next_action": result.get("next_action"),
            "next_tool": result.get("next_tool"),
            "days": result.get("days"),
            "data": _price_data_preview(result.get("data"), 3),
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _diagnosis_brief_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "status": value.get("status"),
            "label": value.get("label"),
            "headline": value.get("headline"),
            "strengths": [_text_excerpt(item, 80) for item in _preview_list(value.get("strengths"), 4)],
            "risks": [_text_excerpt(item, 100) for item in _preview_list(value.get("risks"), 4)],
            "direct_buy_allowed": value.get("direct_buy_allowed"),
            "next_step": _text_excerpt(value.get("next_step"), 160),
        }
    )


def _analyze_stock_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    brief = result.get("diagnosis_brief") if isinstance(result.get("diagnosis_brief"), dict) else {}
    lines = [_analyze_stock_headline(result, brief), _analyze_stock_status_line(result)]
    if brief:
        lines.append(_analyze_stock_action_line(brief))
    elif result.get("data"):
        lines.append(_analyze_stock_price_line(result))
    if handoff_line := _next_tool_brief_line(result.get("next_tool")):
        lines.append(handoff_line)
    return [line for line in lines if line][:max_lines]


def _analyze_stock_headline(result: dict[str, Any], brief: dict[str, Any]) -> str:
    headline = _text_excerpt(brief.get("headline"), 120)
    if headline:
        return headline
    name = " ".join(
        part for part in (str(result.get("code") or "").strip(), str(result.get("name") or "").strip()) if part
    )
    if result.get("data"):
        return f"个股行情: {name}" if name else "个股行情"
    return f"个股诊断: {name}" if name else "个股诊断"


def _analyze_stock_status_line(result: dict[str, Any]) -> str:
    parts = [
        _stock_value_part("现价", result.get("latest_close")),
        _stock_value_part("日期", result.get("latest_date")),
        _stock_value_part("健康", result.get("health")),
        _stock_value_part("均线", result.get("ma_pattern")),
        _stock_value_part("通道", result.get("l2_channel")),
        _stock_value_part("得分", result.get("candidate_score")),
    ]
    return " · ".join(part for part in parts if part)


def _stock_value_part(label: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return f"{label}{_text_excerpt(value, 80)}"


def _analyze_stock_action_line(brief: dict[str, Any]) -> str:
    parts = [
        _stock_list_part("亮点", brief.get("strengths"), 3),
        _stock_list_part("风险", brief.get("risks"), 3),
        _stock_next_step_part(brief.get("next_step")),
    ]
    return " · ".join(part for part in parts if part)


def _analyze_stock_price_line(result: dict[str, Any]) -> str:
    rows = _preview_list(result.get("data"), 999)
    latest = rows[-1] if rows and isinstance(rows[-1], dict) else {}
    parts = [f"行情样本: {len(rows)}条"]
    if pct := _format_pct_signed(latest.get("pct_chg")):
        parts.append(f"最新涨跌{pct}")
    return " · ".join(parts)


def _price_data_preview(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return list(value[-limit:])


def _stock_list_part(label: str, value: Any, limit: int) -> str:
    items = [_text_excerpt(item, 80) for item in _preview_list(value, limit) if str(item).strip()]
    return f"{label}: {'；'.join(items)}" if items else ""


def _stock_next_step_part(value: Any) -> str:
    text = _text_excerpt(value, 100)
    return f"下一步: {text}" if text else ""


def _strategy_decision_preview(result: dict[str, Any]) -> str:
    payload = _drop_empty_preview_fields(
        {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "report_source": result.get("report_source"),
            "candidate_count": result.get("candidate_count"),
            "reviewed_codes": _preview_list(result.get("reviewed_codes"), 12),
            "reviewed_symbols": _candidate_preview_list(result.get("reviewed_symbols"), 12),
            "candidate_conclusion": _candidate_conclusion_preview("last_strategy_decision", result),
            "candidate_guard_summary": _candidate_guard_preview(result.get("candidate_guard_summary")),
            "strategy_policy": _screen_strategy_policy_preview(result.get("strategy_policy")),
            "screen_summary": result.get("screen_summary"),
            "decision_brief": _screen_decision_preview(result.get("decision_brief")),
            "next_action": result.get("next_action"),
            "missing_credentials": _preview_list(result.get("missing_credentials"), 4),
            "message": result.get("message"),
            "report_preview": _text_excerpt(result.get("report_preview"), 1000),
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _ai_report_preview(result: dict[str, Any]) -> str:
    payload = _drop_empty_preview_fields(
        {
            "ok": result.get("ok"),
            "reason": result.get("reason"),
            "model": result.get("model"),
            "stock_count": result.get("stock_count"),
            "reviewed_codes": _preview_list(result.get("reviewed_codes"), 12),
            "reviewed_symbols": _candidate_preview_list(result.get("reviewed_symbols"), 12),
            "candidate_conclusion": _candidate_conclusion_preview("last_ai_report", result),
            "candidate_guard_summary": _candidate_guard_preview(result.get("candidate_guard_summary")),
            "strategy_policy": _screen_strategy_policy_preview(result.get("strategy_policy")),
            "next_action": result.get("next_action"),
            "next_tool": result.get("next_tool"),
            "report_excerpt": _text_excerpt(result.get("report_text"), 1400),
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _ai_report_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    lines = [_tool_stage_line("AI研报", result, _reviewed_count(result))]
    if conclusion_line := _candidate_conclusion_brief_line("last_ai_report", result):
        lines.append(conclusion_line)
    if guard_line := _candidate_guard_brief_line(result.get("candidate_guard_summary")):
        lines.append(guard_line)
    if handoff_line := _next_tool_brief_line(result.get("next_tool")):
        lines.append(handoff_line)
    lines.extend(_reviewed_symbol_lines(result, max_lines=max_lines))
    return [line for line in lines if line][:max_lines]


def _strategy_decision_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    lines = [_strategy_stage_line(result)]
    if conclusion_line := _candidate_conclusion_brief_line("last_strategy_decision", result):
        lines.append(conclusion_line)
    if guard_or_report_line := _strategy_guard_or_report_brief_line(result):
        lines.append(guard_or_report_line)
    lines.extend(_reviewed_symbol_lines(result, max_lines=max_lines))
    return [line for line in lines if line][:max_lines]


def _strategy_guard_or_report_brief_line(result: dict[str, Any]) -> str:
    guard_line = _candidate_guard_brief_line(result.get("candidate_guard_summary"))
    report_line = _strategy_report_action_brief_line(result)
    if guard_line and report_line:
        return _text_excerpt(f"{guard_line} · {report_line}", 320)
    return guard_line or report_line


def _strategy_report_action_brief_line(result: dict[str, Any]) -> str:
    text = str(result.get("report_preview") or "")
    if not text.strip():
        return ""
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip().lstrip("-*• \t")
        if line and _has_strategy_action_keyword(line):
            return f"研报边界: {_text_excerpt(line, 180)}"
    return ""


def _has_strategy_action_keyword(line: str) -> bool:
    return any(keyword in line for keyword in ("触发", "失效", "止损", "突破", "站稳"))


def _strategy_stage_line(result: dict[str, Any]) -> str:
    parts = [
        _strategy_status_label(result),
        _strategy_label_part("来源", _strategy_source_label(result.get("report_source"))),
        _strategy_label_part("已复核", _strategy_reviewed_label(_reviewed_count(result))),
        _strategy_policy_stage_part(result.get("strategy_policy")),
        _strategy_label_part("原因", _strategy_blocker_reason(result)),
        _strategy_missing_credentials_part(result.get("missing_credentials")),
        _strategy_label_part("下一步", result.get("next_action") or result.get("message")),
    ]
    detail = " · ".join(part for part in parts if part)
    return f"攻防决策: {detail}" if detail else ""


def _strategy_status_label(result: dict[str, Any]) -> str:
    status = str(result.get("status") or result.get("reason") or "").strip()
    if label := _strategy_blocker_label(status):
        return label
    if status == "skipped_notify_unconfigured":
        return "未发送工单"
    if result.get("ok") is True:
        return "已完成"
    if result.get("ok") is False:
        return "未完成"
    return status


def _strategy_label_part(label: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return f"{label}: {_text_excerpt(value, 120)}"


def _strategy_missing_credentials_part(value: Any) -> str:
    items = [str(item).strip() for item in _preview_list(value, 4) if str(item).strip()]
    return f"缺配置: {','.join(items)}" if items else ""


def _strategy_source_label(value: Any) -> str:
    source = str(value or "").strip()
    return {
        "last_ai_report": "上一轮AI研报",
        "generated_from_candidates": "候选自动研报",
        "provided": "外部研报",
        "blocked_by_screen_data_quality": "筛选数据质量阻断",
        "blocked_by_screen_quality_gate": "筛选质量门槛阻断",
        "blocked_by_screen_policy_guard": "筛选策略护栏阻断",
        "blocked_by_screen_watch_only": "观察候选阻断",
        "empty": "未提供研报",
    }.get(source, source)


def _strategy_reviewed_label(count: int) -> str:
    return f"{count}只"


def _strategy_blocker_label(status: Any) -> str:
    return {
        "blocked_by_data_quality": "数据质量未过关",
        "blocked_by_quality_gate": "候选质量门槛未过",
        "blocked_by_policy_guard": "候选仍是只读观察",
    }.get(str(status or "").strip(), "")


def _strategy_blocker_reason(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").strip()
    if not status.startswith("blocked_"):
        return ""
    return str(result.get("reason") or "").strip()


def _tool_stage_line(label: str, result: dict[str, Any], reviewed: int) -> str:
    parts = [
        _key_value("reviewed", reviewed),
        _key_value("model", result.get("model")),
        _strategy_policy_stage_part(result.get("strategy_policy")),
        _key_value("next", result.get("next_action") or result.get("reason")),
    ]
    detail = ", ".join(part for part in parts if part)
    return f"{label}: {detail}" if detail else ""


def _strategy_policy_stage_part(value: Any) -> str:
    line = _screen_strategy_policy_line(value)
    return line.replace("策略治理: ", "策略治理=", 1) if line else ""


def _candidate_guard_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "direct_buy_blocked_count": value.get("direct_buy_blocked_count"),
            "message": value.get("message"),
            "candidates": _candidate_guard_candidate_preview(value.get("candidates")),
        }
    )


def _candidate_guard_candidate_preview(value: Any) -> list[dict[str, Any]]:
    rows = _preview_list(value, 5)
    return [
        _drop_empty_preview_fields(
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "reason": row.get("reason"),
                "action_status": row.get("action_status"),
                **_candidate_action_preview_fields(row),
                "label_ready": row.get("label_ready"),
                "risk_factors": _preview_list(row.get("risk_factors"), 3),
                "next_step": _text_excerpt(row.get("next_step"), 120),
            }
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _candidate_guard_brief_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    candidates = [row for row in _preview_list(value.get("candidates"), 3) if isinstance(row, dict)]
    count = value.get("direct_buy_blocked_count") or len(candidates)
    detail = "、".join(_candidate_guard_candidate_line(row) for row in candidates[:2])
    detail = detail.strip("、")
    head = f"候选护栏: {count}只禁止直接买入"
    return f"{head} · {detail}" if detail else head


def _candidate_guard_candidate_line(row: dict[str, Any]) -> str:
    name = _candidate_name(row)
    reason = _text_excerpt(row.get("reason"), 60)
    return f"{name}({reason})" if reason else name


def _key_value(key: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return f"{key}={_text_excerpt(value, 120)}"


def _reviewed_count(result: dict[str, Any]) -> int:
    try:
        count = int(result.get("stock_count") or result.get("candidate_count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count:
        return count
    return max(
        len(_preview_list(result.get("reviewed_codes"), 20)), len(_preview_list(result.get("reviewed_symbols"), 20))
    )


def _reviewed_symbol_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    rows = _dedupe_candidate_rows(_preview_list(result.get("reviewed_symbols"), max_lines))
    return [line for row in rows if (line := _candidate_brief_line(row))]


def _candidate_conclusion_preview(source_stage: str, result: dict[str, Any]) -> dict[str, Any]:
    row = _candidate_conclusion_row(source_stage, result)
    if not row:
        return {}
    action_plan = _candidate_conclusion_action_plan(result)
    action_fields = _candidate_action_preview_fields({**action_plan, **row})
    return _drop_empty_preview_fields(
        {
            "line": _candidate_conclusion_line(row, result),
            "code": str(row.get("code") or "").strip(),
            "name": str(row.get("name") or "").strip(),
            "scores": _candidate_conclusion_score_preview(row),
            **action_fields,
            "trade_readiness": str(row.get("trade_readiness") or action_plan.get("trade_readiness") or "").strip(),
            "new_buy_allowed": row.get("new_buy_allowed")
            if row.get("new_buy_allowed") is not None
            else action_plan.get("new_buy_allowed"),
            "evidence": _candidate_conclusion_evidence_items(row),
            "quality_factors": _candidate_quality_text_items(row, 5, 100),
            "risk_factors": _candidate_risk_text_items(row, 5, 100),
            "guard_reason": _candidate_conclusion_guard_reason(row, result),
            "next_step": _candidate_conclusion_next_step(row, result),
            "source_stage": source_stage,
        }
    )


def _candidate_conclusion_score_preview(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "funnel_score",
        "candidate_shadow_score",
        "candidate_shadow_grade",
        "entry_quality_score",
        "entry_quality_grade",
        "candidate_quality_score",
        "risk_adjusted_quality_score",
        "entry_risk_penalty",
    )
    return _drop_empty_preview_fields({key: row.get(key) for key in keys})


def _candidate_action_preview_fields(row: dict[str, Any]) -> dict[str, Any]:
    if not any(key in row for key in ("action_status", "status", "direct_buy_allowed")):
        return {}
    return candidate_action_fields(row)


def _candidate_conclusion_brief_line(source_stage: str, result: dict[str, Any]) -> str:
    conclusion = _candidate_conclusion_preview(source_stage, result)
    return _text_excerpt(conclusion.get("line"), 280) if conclusion else ""


def _candidate_conclusion_row(source_stage: str, result: dict[str, Any]) -> dict[str, Any]:
    rows = _candidate_conclusion_candidates(source_stage, result)
    first = _first_candidate_row(rows)
    if not first:
        return {}
    code = str(first.get("code") or "").strip()
    merged: dict[str, Any] = {}
    for row in rows:
        if code and str(row.get("code") or "").strip() != code:
            continue
        merged.update(_drop_empty_preview_fields(row))
    merged.update(_drop_empty_preview_fields(first))
    if source_stage == "last_screen_result":
        return _merge_screen_preference_miss_risks(merged, result)
    return merged


def _merge_screen_preference_miss_risks(row: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    risks = _screen_candidate_preference_miss_risk_texts(row, result)
    if not risks:
        return row
    existing = [str(item) for item in _preview_list(row.get("risk_factors"), 8) if str(item).strip()]
    merged = list(dict.fromkeys([*existing, *risks]))
    return {**row, "risk_factors": merged}


def _screen_candidate_preference_miss_risk_texts(row: dict[str, Any], result: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    if style_text := _screen_missing_style_preference_text(row, result.get("style_preference")):
        risks.append(_preference_miss_risk("风格偏好未命中", style_text))
    if _has_theme_preference(result.get("theme_preference")) and not _candidate_matches_preference(row, "theme"):
        risks.append(_preference_miss_risk("主题偏好未命中", _theme_preference_text(result.get("theme_preference"))))
    return [risk for risk in risks if risk]


def _screen_missing_style_preference_text(row: dict[str, Any], value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if not _style_preference_styles(value):
        return (
            ""
            if not _has_style_preference(value) or _candidate_matches_preference(row, "style")
            else _style_preference_text(value)
        )
    return "/".join(_candidate_missing_style_preference_labels(row, value))


def _preference_miss_risk(label: str, value: str) -> str:
    return f"{label}: {value}" if value else label


def _candidate_conclusion_candidates(source_stage: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    if source_stage == "last_screen_result":
        return _screen_candidate_rows(result)
    if source_stage == "last_recommendation_event_eval":
        selection = result.get("policy_selection") if isinstance(result.get("policy_selection"), dict) else {}
        return _ranked_candidate_rows(selection.get("picks"), 12, 4)
    return [
        *_ranked_candidate_rows(_candidate_guard_rows(result), 5, 4),
        *_ranked_candidate_rows(result.get("reviewed_symbols"), 12, 4),
    ]


def _candidate_guard_rows(result: dict[str, Any]) -> list[Any]:
    guard = result.get("candidate_guard_summary") if isinstance(result.get("candidate_guard_summary"), dict) else {}
    return _preview_list(guard.get("candidates"), 5) if isinstance(guard, dict) else []


def _first_candidate_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [row for row in rows if row.get("code") or row.get("name") or row.get("summary")]
    if not candidates:
        return {}
    return max(enumerate(candidates), key=lambda item: _candidate_rank_key(item[1], item[0]))[1]


def _candidate_rank_key(row: dict[str, Any], index: int) -> tuple[int, int, int, float, int]:
    return (
        _candidate_source_rank(row),
        _candidate_status_rank(row),
        1 if row.get("selected_for_report") is True or row.get("is_ai_recommended") is True else 0,
        _candidate_best_score(row),
        -index,
    )


def _candidate_source_rank(row: dict[str, Any]) -> int:
    try:
        return int(row.get("_preview_source_rank") or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_status_rank(row: dict[str, Any]) -> int:
    status = str(row.get("action_status") or row.get("status") or "").strip()
    if status == "ready_for_ai_review":
        return 4
    if status in {"candidate", "review_ready"}:
        return 3
    if status == "watch_only":
        return 2
    if status.startswith("blocked_"):
        return 1
    return 0


def _candidate_best_score(row: dict[str, Any]) -> float:
    scores = (
        row.get("candidate_shadow_score"),
        row.get("risk_adjusted_quality_score"),
        row.get("candidate_quality_score"),
        row.get("entry_quality_score"),
        row.get("funnel_score"),
        row.get("priority_score"),
        row.get("shadow_score"),
        row.get("score"),
    )
    values = [_score_float(value) for value in scores]
    return max((value for value in values if value is not None), default=0.0)


def _candidate_conclusion_line(row: dict[str, Any], result: dict[str, Any]) -> str:
    guard_reason = _candidate_conclusion_guard_reason(row, result)
    line = f"候选结论: {_candidate_conclusion_prefix(row, guard_reason)} {_candidate_brief_line(row)}"
    next_step = _candidate_conclusion_next_step(row, result)
    if guard_reason:
        guard_text = f"护栏: {guard_reason}"
        next_text = f"下一步: {next_step}" if next_step else ""
        if next_text and f" · {next_text}" in line:
            line = line.replace(f" · {next_text}", f" · {guard_text} · {next_text}", 1)
        elif guard_text not in line:
            line += f" · {guard_text}"
    if next_step and f"下一步: {next_step}" not in line:
        line += f" · 下一步: {next_step}"
    return line


def _candidate_conclusion_prefix(row: dict[str, Any], guard_reason: str = "") -> str:
    return candidate_action_role(
        row.get("action_status"),
        guard_reason=guard_reason,
        has_code=bool(str(row.get("code") or "").strip()),
    )


def _candidate_conclusion_evidence_items(row: dict[str, Any]) -> list[str]:
    text = _brief_evidence(row)
    if not text:
        return []
    return [part.strip() for part in text.removeprefix("证据: ").split("；") if part.strip()]


def _candidate_conclusion_text_items(value: Any, limit: int, clip: int) -> list[str]:
    return [_text_excerpt(item, clip) for item in _preview_list(value, limit) if str(item or "").strip()]


def _candidate_quality_text_items(row: dict[str, Any], limit: int, clip: int) -> list[str]:
    factors: list[str] = []
    for value in (row.get("style_match_reasons"), row.get("theme_match_reasons"), row.get("quality_factors")):
        for item in _candidate_conclusion_text_items(value, limit, clip):
            if item not in factors:
                factors.append(item)
            if len(factors) >= limit:
                return factors
    return factors


def _candidate_risk_text_items(row: dict[str, Any], limit: int, clip: int) -> list[str]:
    risks: list[str] = []
    for value in (row.get("risk_factors"), row.get("entry_quality_risk_flags"), row.get("daily_trap_reason")):
        for item in _candidate_risk_value_items(value, limit, clip):
            if item not in risks:
                risks.append(item)
            if len(risks) >= limit:
                return risks
    return risks


def _candidate_risk_value_items(value: Any, limit: int, clip: int) -> list[str]:
    if isinstance(value, list):
        return _candidate_conclusion_text_items(value, limit, clip)
    text = _text_excerpt(value, clip)
    return [text] if text.strip() else []


def _candidate_conclusion_guard_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    guard = result.get("candidate_guard_summary") if isinstance(result.get("candidate_guard_summary"), dict) else {}
    candidates = _preview_list(guard.get("candidates"), 5) if isinstance(guard, dict) else []
    code = str(row.get("code") or "").strip()
    for item in candidates:
        if isinstance(item, dict) and str(item.get("code") or "").strip() == code and item.get("reason"):
            return str(item["reason"])
    if not code:
        first = next((item for item in candidates if isinstance(item, dict) and item.get("reason")), {})
        if first:
            return str(first["reason"])
    if reason := _candidate_conclusion_action_reason(result):
        return reason
    return _candidate_conclusion_row_guard_reason(row)


def _candidate_conclusion_row_guard_reason(row: dict[str, Any]) -> str:
    reason = candidate_guard_reason(row)
    return "" if reason.startswith("候选状态 ") else reason


def _candidate_conclusion_action_reason(result: dict[str, Any]) -> str:
    action_plan = _candidate_conclusion_action_plan(result)
    review_targets = action_plan.get("review_targets") if isinstance(action_plan.get("review_targets"), dict) else {}
    data_gate = action_plan.get("data_quality_gate") if isinstance(action_plan.get("data_quality_gate"), dict) else {}
    quality_gate = action_plan.get("quality_gate") if isinstance(action_plan.get("quality_gate"), dict) else {}
    top_level_quality_gate = result.get("quality_gate") if isinstance(result.get("quality_gate"), dict) else {}
    return str(
        review_targets.get("reason")
        or quality_gate.get("reason")
        or top_level_quality_gate.get("reason")
        or data_gate.get("reason")
        or action_plan.get("reason")
        or ""
    )


def _candidate_conclusion_next_step(row: dict[str, Any], result: dict[str, Any]) -> str:
    action_plan = _candidate_conclusion_action_plan(result)
    next_step = row.get("next_step") or result.get("next_action") or action_plan.get("next_step")
    return str(next_step or "")


def _candidate_conclusion_action_plan(result: dict[str, Any]) -> dict[str, Any]:
    selection = result.get("policy_selection") if isinstance(result.get("policy_selection"), dict) else {}
    nested = selection.get("action_plan") if isinstance(selection.get("action_plan"), dict) else {}
    if nested:
        return nested
    return result.get("action_plan") if isinstance(result.get("action_plan"), dict) else {}


def _screen_stocks_preview(result: dict[str, Any]) -> str:
    payload = _drop_empty_preview_fields(
        {
            "ok": result.get("ok"),
            "job_kind": result.get("job_kind"),
            "board": result.get("board"),
            "scan_scope": result.get("scan_scope"),
            "style_preference": result.get("style_preference"),
            "theme_preference": result.get("theme_preference"),
            "preference_match": _screen_preference_match_preview(result),
            "summary": result.get("summary"),
            "data_quality": result.get("data_quality"),
            "trade_mode": result.get("trade_mode"),
            "strategy_policy": _screen_strategy_policy_preview(result.get("strategy_policy")),
            "theme_context": _screen_theme_context_preview(result.get("theme_context")),
            "etf_enhancement": _screen_etf_enhancement_preview(result.get("etf_enhancement")),
            "etf_candidates": _screen_etf_candidate_preview_list(result.get("etf_candidates"), 6),
            "decision_brief": _screen_decision_preview(result.get("decision_brief")),
            "selection_brief": _screen_selection_preview(result.get("selection_brief")),
            "decision_state": _screen_decision_state_preview(result.get("decision_state")),
            "next_action": result.get("next_action"),
            "next_tool": result.get("next_tool"),
            "candidate_guard_summary": _candidate_guard_preview(result.get("candidate_guard_summary")),
            "candidate_conclusion": _candidate_conclusion_preview("last_screen_result", result),
            "top_candidates": _candidate_preview_list(result.get("top_candidates"), 10),
            "symbols_for_report": _candidate_preview_list(result.get("symbols_for_report"), 12),
            "report_candidates": _candidate_preview_list(result.get("report_candidates"), 12),
            "watch_candidates": _candidate_preview_list(result.get("watch_candidates"), 6),
            "diagnosis_targets": _preview_list(result.get("diagnosis_targets"), 6),
            "quality_gate": result.get("quality_gate"),
            "action_plan": _screen_action_plan_preview(result.get("action_plan")),
            "top_sectors": _preview_list(result.get("top_sectors"), 6),
            "omitted": "完整 trigger_groups 已保留在完整结果中" if result.get("trigger_groups") else "",
        }
    )
    return serialize_tool_result(payload) if payload else ""


def _screen_stocks_brief_lines(result: dict[str, Any], *, max_lines: int) -> list[str]:
    lines: list[str] = []
    selection = result.get("selection_brief") if isinstance(result.get("selection_brief"), dict) else {}
    headline = _text_excerpt(selection.get("headline"), 120)
    scope_part = _screen_scope_brief_line(result.get("scan_scope"))
    data_line = _screen_data_quality_brief_part(result.get("data_quality"))
    preference_line = _screen_preference_brief_line(result)
    scope_line = "；".join(part for part in (scope_part, data_line) if part)
    if scope_line and preference_line:
        scope_line = f"{scope_line}；{preference_line}"
    decision_line = _screen_decision_state_line(result.get("decision_state"))
    conclusion = _candidate_conclusion_preview("last_screen_result", result)
    conclusion_line = _text_excerpt(conclusion.get("line"), 280)
    guard_line = _candidate_guard_brief_line(result.get("candidate_guard_summary"))
    etf_line = _screen_etf_brief_line(result.get("etf_enhancement"), result.get("etf_candidates"))
    handoff_line = _screen_review_chain_line(result) or _next_tool_brief_line(result.get("next_tool"))
    reserved = int(bool(conclusion_line)) + int(bool(guard_line)) + int(bool(etf_line)) + int(bool(handoff_line))
    if scope_line and len(lines) < max_lines:
        lines.append(scope_line)
    if headline and len(lines) < max(max_lines - reserved, 0):
        lines.append(headline)
    if preference_line and not scope_line and len(lines) < max(max_lines - reserved, 0):
        lines.append(preference_line)
    if decision_line and len(lines) < max(max_lines - reserved, 0):
        lines.append(decision_line)
    if theme_line := _screen_theme_context_line(result.get("theme_context")):
        if len(lines) < max(max_lines - reserved, 0):
            lines.append(theme_line)
    if policy_line := _screen_strategy_policy_line(result.get("strategy_policy")):
        if max_lines > 3 and len(lines) < max(max_lines - reserved, 0):
            lines.append(policy_line)
    if etf_line:
        lines.append(etf_line)
    if handoff_line:
        lines.append(handoff_line)
    if conclusion_line:
        lines.append(conclusion_line)
    if guard_line:
        lines.append(guard_line)
    for row in _screen_brief_candidates(result):
        if _candidate_matches_conclusion(row, conclusion):
            continue
        if line := _candidate_brief_line(row):
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


def _screen_strategy_policy_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    dynamic_mode = value.get("dynamic_mode")
    execution_policy = value.get("execution_policy")
    next_action = value.get("next_action")
    return _drop_empty_preview_fields(
        {
            "dynamic_mode": _policy_execution_preview(dynamic_mode),
            "dynamic_mode_raw": dynamic_mode,
            "execution_policy": _policy_execution_preview(execution_policy),
            "execution_policy_raw": execution_policy,
            "active_scope": value.get("policy_weight_active_scope"),
            "selection_action_count": value.get("selection_action_count"),
            "selection_action_summary": _text_excerpt(value.get("selection_action_summary"), 180),
            "formal_dynamic_allowed": value.get("formal_dynamic_allowed"),
            "next_action": _policy_next_action_preview(next_action),
            "next_action_raw": next_action,
            "signal_weights": value.get("signal_weights"),
            "attribution_signal_weights": value.get("attribution_signal_weights"),
        }
    )


def _policy_execution_preview(value: Any) -> str:
    return policy_execution_mode_label(value) if str(value or "").strip() else ""


def _policy_next_action_preview(value: Any) -> str:
    return policy_next_action_label(value) if str(value or "").strip() else ""


def _screen_strategy_policy_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    summary = str(value.get("selection_action_summary") or "").strip()
    if summary and summary != "候选源治理=无":
        return f"策略治理: {_text_excerpt(summary, 180)}"
    weights = value.get("attribution_signal_weights")
    if isinstance(weights, dict) and weights:
        return f"策略治理: 归因调权 {','.join(str(k) for k in list(weights)[:6])}"
    return ""


def _screen_review_chain_line(result: dict[str, Any]) -> str:
    handoff = result.get("next_tool") if isinstance(result.get("next_tool"), dict) else {}
    if handoff.get("tool") != "generate_ai_report":
        return ""
    diagnosis_call = _screen_diagnosis_call(result.get("diagnosis_targets"))
    if not diagnosis_call:
        return ""
    report_call = _next_tool_call_text(handoff)
    return f"复核链路: {diagnosis_call} → {report_call} · 先结构诊断，再研报复核"


def _screen_diagnosis_call(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    for row in value:
        if not isinstance(row, dict) or row.get("tool") != "analyze_stock":
            continue
        return _next_tool_call_text(row)
    return ""


def _screen_etf_enhancement_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = ("pool", "fetched", "l2_passed", "strong_candidates", "boosted_sectors")
    return _drop_empty_preview_fields({key: value.get(key) for key in keys})


def _screen_etf_candidate_preview_list(value: Any, limit: int) -> list[dict[str, Any]]:
    rows = _preview_list(value, limit)
    return [_screen_etf_candidate_preview(row) for row in rows if isinstance(row, dict)]


def _screen_etf_candidate_preview(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("code", "name", "sector", "score", "ret3", "ret20", "vol_ratio", "channel")
    return _drop_empty_preview_fields({key: row.get(key) for key in keys})


def _screen_etf_brief_line(metrics: Any, candidates: Any) -> str:
    if not isinstance(metrics, dict) and not isinstance(candidates, list):
        return ""
    names = [_candidate_name(row) for row in _preview_list(candidates, 3) if isinstance(row, dict)]
    if not names and not any((metrics or {}).get(key) for key in ("pool", "fetched", "l2_passed", "strong_candidates")):
        return ""
    pool = _safe_int_text((metrics or {}).get("pool") if isinstance(metrics, dict) else None)
    fetched = _safe_int_text((metrics or {}).get("fetched") if isinstance(metrics, dict) else None)
    l2_passed = _safe_int_text((metrics or {}).get("l2_passed") if isinstance(metrics, dict) else None)
    head = f"ETF强势池: 池{pool or '-'} → 拉取{fetched or '-'} → L2强势{l2_passed or '-'}"
    return f"{head}；候选: {', '.join(name for name in names if name)}" if names else head


def _next_tool_brief_line(value: Any) -> str:
    call = _next_tool_call_text(value)
    if not call:
        return ""
    reason = _text_excerpt(value.get("reason"), 80) if isinstance(value, dict) else ""
    return f"下一工具: {call} · {reason}" if reason else f"下一工具: {call}"


def _next_tool_call_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    tool = str(value.get("tool") or "").strip()
    if not tool:
        return ""
    args = _next_tool_args_text(value.get("args"))
    return f"{tool}({args})" if args else f"{tool}()"


def _next_tool_args_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key, raw in value.items():
        text = _next_tool_arg_value_text(raw)
        if text:
            parts.append(f"{key}={text}")
        if len(parts) >= 3:
            break
    return ", ".join(parts)


def _next_tool_arg_value_text(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item) for item in _preview_list(value, 5) if str(item).strip()]
        return ",".join(items)
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        return _text_excerpt(json.dumps(value, ensure_ascii=False, default=str), 80)
    return _text_excerpt(value, 80)


def _screen_scope_brief_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    board = str(value.get("board") or "all").strip()
    total = _safe_int_text(value.get("total_scanned"))
    limit = _safe_int_text(value.get("limit"))
    financial = _screen_financial_scope_suffix(value)
    if str(value.get("scope") or "").strip() == "bounded" or (limit and limit != "0"):
        suffix = f"前{limit}只" if limit else "有界扫描"
        return f"快扫: {board} {suffix}，实际扫描{total or '-'}只{financial}"
    return f"全量: {board} 扫描{total or '-'}只{financial}"


def _screen_data_quality_brief_part(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    status = str(value.get("status") or "").strip()
    date_text = str(value.get("end_trade_date") or "").strip()
    coverage = _safe_percent_text(value.get("coverage_pct"))
    status_label = _data_quality_status_label(status)
    coverage_text = f"覆盖{coverage}{status_label}" if coverage else status_label
    details = [date_text, coverage_text]
    text = " ".join(part for part in details if part)
    if not text:
        return ""
    warnings = [str(item).strip() for item in _preview_list(value.get("warnings"), 2) if str(item).strip()]
    warning_text = f"，{'；'.join(warnings)}" if status in {"partial", "degraded", "empty"} and warnings else ""
    return f"数据: {text}{warning_text}"


def _safe_percent_text(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return ""


def _data_quality_status_label(status: str) -> str:
    return {
        "ok": "(可靠)",
        "partial": "(部分)",
        "degraded": "(降级)",
        "empty": "(无数据)",
    }.get(status, f"({status})" if status else "")


def _screen_financial_scope_suffix(value: dict[str, Any]) -> str:
    mode = str(value.get("financial_metrics") or "").strip()
    count = _safe_int_text(value.get("financial_metrics_count"))
    if mode == "skipped_quick_scan":
        return "，财务过滤: 快扫跳过"
    if mode == "available":
        return f"，财务过滤: {count}只" if count else "，财务过滤: 已启用"
    if mode == "requested_unavailable":
        return "，财务过滤: 未取得"
    return ""


def _screen_preference_brief_line(result: dict[str, Any]) -> str:
    match = _screen_preference_match_preview(result)
    parts = [
        _preference_part("风格", _style_preference_text(result.get("style_preference")), match.get("style")),
        _preference_part("主题", _theme_preference_text(result.get("theme_preference")), match.get("theme")),
    ]
    if alternatives := _screen_preference_alternative_text(result.get("selection_brief")):
        parts.append(alternatives)
    text = "；".join(part for part in parts if part)
    return f"筛选偏好: {text}" if text else ""


def _screen_preference_alternative_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    names = [
        _candidate_name(row) for row in _preview_list(value.get("preference_alternatives"), 3) if isinstance(row, dict)
    ]
    return f"偏好命中观察: {', '.join(name for name in names if name)}" if names else ""


def _preference_part(label: str, text: str, match_status: str = "") -> str:
    suffix = {"miss": "(未命中)", "partial": "(部分命中)"}.get(match_status, "")
    return f"{label}={text}{suffix}" if text else ""


def _screen_preference_match_preview(result: dict[str, Any]) -> dict[str, str]:
    existing = result.get("preference_match")
    if isinstance(existing, dict) and existing:
        return _drop_empty_preview_fields(
            {
                "style": existing.get("style"),
                "theme": existing.get("theme"),
            }
        )
    rows = _screen_candidate_rows(result)
    return _drop_empty_preview_fields(
        {
            "style": _screen_style_preference_match_status(rows, result.get("style_preference")),
            "theme": _preference_match_status(rows, "theme")
            if _has_theme_preference(result.get("theme_preference"))
            else "",
        }
    )


def _style_preference_styles(value: Any) -> list[str]:
    return [str(item) for item in _preview_list(value.get("styles"), 4) if str(item)] if isinstance(value, dict) else []


def _screen_decision_state_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "status": value.get("status"),
            "label": value.get("label"),
            "trade_readiness": value.get("trade_readiness"),
            "new_buy_allowed": value.get("new_buy_allowed"),
            "candidate_direct_buy_allowed": value.get("candidate_direct_buy_allowed"),
            "candidate_guard_reason": _text_excerpt(value.get("candidate_guard_reason"), 140),
            "ai_review_allowed": value.get("ai_review_allowed"),
            "primary": value.get("primary"),
            "reason": _text_excerpt(value.get("reason"), 140),
            "next_step": _text_excerpt(value.get("next_step"), 140),
            "summary": _text_excerpt(value.get("summary"), 220),
        }
    )


def _screen_decision_state_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return _text_excerpt(value.get("summary"), 220)


def _safe_int_text(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return ""


def _candidate_matches_conclusion(row: dict[str, Any], conclusion: dict[str, Any]) -> bool:
    if not conclusion:
        return False
    code = str(conclusion.get("code") or "").strip()
    if code and str(row.get("code") or "").strip() == code:
        return True
    name = str(conclusion.get("name") or "").strip()
    return bool(name and str(row.get("name") or "").strip() == name)


def _screen_brief_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    return _dedupe_candidate_rows(_screen_candidate_rows(result))


def _screen_candidate_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    selection = result.get("selection_brief") if isinstance(result.get("selection_brief"), dict) else {}
    rows: list[dict[str, Any]] = []
    rows.extend(_ranked_candidate_rows(result.get("report_candidates"), 3, 5))
    rows.extend(_ranked_candidate_rows(result.get("symbols_for_report"), 3, 5))
    if isinstance(selection.get("primary_pick"), dict):
        rows.append(_rank_candidate_row(selection["primary_pick"], 4))
    rows.extend(_ranked_candidate_rows(selection.get("best_candidates"), 3, 4))
    rows.extend(_ranked_candidate_rows(result.get("watch_candidates"), 3, 2))
    rows.extend(_ranked_candidate_rows(result.get("top_candidates"), 3, 1))
    return rows


def _ranked_candidate_rows(value: Any, limit: int, source_rank: int) -> list[dict[str, Any]]:
    return [_rank_candidate_row(row, source_rank) for row in _preview_list(value, limit) if isinstance(row, dict)]


def _rank_candidate_row(row: dict[str, Any], source_rank: int) -> dict[str, Any]:
    return {**row, "_preview_source_rank": source_rank}


def _dedupe_candidate_rows(rows: list[Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("code") or row.get("summary") or row.get("name") or row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _candidate_brief_line(row: dict[str, Any]) -> str:
    name = _candidate_name(row)
    parts = [
        _action_status_label(row.get("action_status")),
        _brief_evidence(row),
        _brief_theme(row),
        _brief_quality(row),
        _brief_risk(row),
        _brief_next_step(row),
    ]
    detail = " · ".join(part for part in parts if part)
    return f"{name} · {detail}" if detail else name


def _candidate_name(row: dict[str, Any]) -> str:
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip()
    return " ".join(part for part in (code, name) if part) or str(row.get("summary") or "候选").strip()


def _action_status_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    label = candidate_action_label(text)
    return label if label != text or text.startswith("blocked_") else ""


def _brief_risk(row: dict[str, Any]) -> str:
    risks = _candidate_risk_text_items(row, 2, 80)
    return f"风险: {'；'.join(risks)}" if risks else ""


def _brief_evidence(row: dict[str, Any]) -> str:
    parts = [
        _score_evidence("优先分", row.get("priority_score")),
        _score_evidence("动态分", row.get("shadow_score")),
        _score_evidence("触发分", row.get("score")),
        _score_evidence("漏斗分", row.get("funnel_score")),
        _grade_score_evidence("候选影子", row.get("candidate_shadow_grade"), row.get("candidate_shadow_score")),
        _grade_score_evidence("入场", row.get("entry_quality_grade"), row.get("entry_quality_score")),
        _score_evidence("风险调整分", row.get("risk_adjusted_quality_score")),
        "已AI推荐" if row.get("is_ai_recommended") is True else "",
        _strategy_evidence(row.get("selection_strategy")),
    ]
    evidence = [part for part in parts if part]
    return f"证据: {'；'.join(evidence[:5])}" if evidence else ""


def _score_evidence(label: str, value: Any) -> str:
    try:
        raw_score = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(raw_score) or raw_score <= 0:
        return ""
    score = _format_score(raw_score)
    return f"{label}{score}" if score else ""


def _grade_score_evidence(label: str, grade: Any, score: Any) -> str:
    grade_text = str(grade or "").strip()
    score_text = _format_score(score)
    if grade_text and score_text:
        return f"{label}{grade_text}/{score_text}"
    if grade_text:
        return f"{label}{grade_text}"
    if score_text:
        return f"{label}{score_text}"
    return ""


def _strategy_evidence(value: Any) -> str:
    strategy = str(value or "").strip()
    return f"排序:{strategy}" if strategy else ""


def _format_score(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(score):
        return ""
    return f"{score:.2f}".rstrip("0").rstrip(".")


def _brief_quality(row: dict[str, Any]) -> str:
    factors = _candidate_quality_text_items(row, 2, 80)
    if factors:
        return f"亮点: {'；'.join(factors)}"
    text = _text_excerpt(row.get("why") or row.get("evidence") or row.get("rank_reason"), 80)
    return f"亮点: {text}" if text else ""


def _brief_theme(row: dict[str, Any]) -> str:
    theme = str(row.get("strategic_theme") or row.get("theme") or "").strip()
    if not theme:
        return ""
    source = str(row.get("theme_source") or "").strip()
    label = "事件主线" if source == "ths_hot_event" else "主题"
    reason = _text_excerpt(row.get("theme_event_reason"), 40)
    return f"{label}: {theme}({reason})" if reason else f"{label}: {theme}"


def _brief_next_step(row: dict[str, Any]) -> str:
    next_step = _text_excerpt(row.get("next_step"), 80)
    return f"下一步: {next_step}" if next_step else ""


def _screen_theme_context_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "event_mainlines": _text_excerpt(value.get("event_mainlines"), 240),
            "today_activity": _text_excerpt(value.get("today_activity"), 240),
            "theme_radar": _text_excerpt(value.get("theme_radar"), 240),
            "theme_radar_source": value.get("theme_radar_source"),
            "hot_concepts": _preview_list(value.get("hot_concepts"), 6),
        }
    )


def _screen_theme_context_line(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts = [
        _theme_context_part("事件主线", value.get("event_mainlines")),
        _theme_context_part("异动主题", value.get("today_activity")),
        _theme_context_part("中长线", value.get("theme_radar")),
    ]
    parts = [part for part in parts if part]
    return f"主题上下文: {'；'.join(parts[:2])}" if parts else ""


def _theme_context_part(label: str, value: Any) -> str:
    text = _text_excerpt(value, 80)
    return f"{label}: {text}" if text else ""


def _screen_decision_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "market_gate": value.get("market_gate"),
            "next_action": value.get("next_action"),
            "report_focus": _candidate_preview_list(value.get("report_focus"), 6),
            "watch_focus": _candidate_preview_list(value.get("watch_focus"), 6),
        }
    )


def _screen_selection_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "status": value.get("status"),
            "headline": value.get("headline"),
            "best_codes": _preview_list(value.get("best_codes"), 12),
            "primary_pick": _candidate_preview_item(value.get("primary_pick")),
            "best_candidates": _candidate_preview_list(value.get("best_candidates"), 6),
            "preference_alternatives": _candidate_preview_list(value.get("preference_alternatives"), 6),
            "tool_handoff": value.get("tool_handoff"),
        }
    )


def _screen_action_plan_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {
            "primary_action": value.get("primary_action"),
            "candidate_action": value.get("candidate_action"),
            "new_buy_allowed": value.get("new_buy_allowed"),
            "ai_review_allowed": value.get("ai_review_allowed"),
            "trade_readiness": value.get("trade_readiness"),
            "data_quality_gate": value.get("data_quality_gate"),
            "quality_gate": value.get("quality_gate"),
            "review_targets": value.get("review_targets"),
            "diagnosis_targets": _preview_list(value.get("diagnosis_targets"), 6),
            "report_candidates": _candidate_preview_list(value.get("report_candidates"), 6),
            "watch_candidates": _candidate_preview_list(value.get("watch_candidates"), 6),
        }
    )


_CANDIDATE_PREVIEW_FIELDS = (
    "code",
    "name",
    "summary",
    "tier",
    "quality",
    "track",
    "stage",
    "candidate_lane",
    "entry_type",
    "strategic_theme",
    "theme_score",
    "theme_source",
    "theme_event_id",
    "theme_event_date",
    "theme_event_title",
    "theme_event_reason",
    "style_match",
    "style_match_styles",
    "style_match_score",
    "style_match_reasons",
    "theme_match",
    "theme_match_score",
    "theme_match_reasons",
    "selection_source",
    "source_type",
    "priority_rank",
    "priority_score",
    "shadow_score",
    "score",
    "selection_strategy",
    "recommend_date",
    "is_ai_recommended",
    "selected_for_report",
    "raw_selected_for_report",
    "funnel_score",
    "recommend_count",
    "candidate_shadow_score",
    "candidate_shadow_grade",
    "entry_quality_score",
    "entry_quality_grade",
    "entry_quality_risk_flags",
    "candidate_quality_score",
    "risk_adjusted_quality_score",
    "entry_risk_penalty",
    "label_ready",
    "label_status",
    "rank_reason",
    "quality_factors",
    "risk_factors",
    "daily_trap_reason",
    "action_status",
    "why",
    "evidence",
    "next_step",
    "triggers",
)


def _candidate_preview_list(value: Any, limit: int) -> list[Any]:
    rows = _preview_list(value, limit)
    return [_candidate_preview_item(row) if isinstance(row, dict) else row for row in rows]


def _candidate_preview_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _drop_empty_preview_fields(
        {field: _candidate_preview_value(field, value.get(field)) for field in _CANDIDATE_PREVIEW_FIELDS}
    )


def _candidate_preview_value(field: str, value: Any) -> Any:
    if isinstance(value, list):
        return [_text_excerpt(item, 80) for item in value[:8] if str(item).strip()]
    if field in {"summary", "rank_reason", "why", "evidence", "next_step", "theme_event_title", "theme_event_reason"}:
        return _text_excerpt(value, 240)
    return value


def _preview_list(value: Any, limit: int) -> list[Any]:
    return list(value[:limit]) if isinstance(value, list) else []


def _text_excerpt(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
