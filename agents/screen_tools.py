"""Agent-facing Wyckoff funnel screen tool."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from agents.tool_context import ToolContext, ensure_tushare_token
from core.candidate_actions import candidate_action_fields
from core.candidate_guards import candidate_guard_summary
from core.candidate_metadata import build_candidate_metadata_map, code6
from core.candidate_policy import candidate_score_value
from core.candidate_preference import (
    candidate_matches_preference as _candidate_matches_preference,
)
from core.candidate_preference import (
    candidate_style_match_styles as _candidate_style_match_styles,
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
    style_preference_match_status as _style_preference_match_status,
)
from core.candidate_quality import (
    ai_review_quality_gate_reason,
    candidate_ai_review_label,
    entry_quality_risk_flags,
    entry_quality_risk_penalty,
    risk_adjusted_quality_metrics,
    risk_adjusted_quality_score,
    split_ai_review_candidates,
)
from core.candidate_ranker import TRIGGER_SHORT_LABELS
from core.candidate_tracks import candidate_entry_track
from core.funnel_taxonomy import lane_label, source_label
from core.strategy_policy_display import policy_execution_mode_label, policy_next_action_label
from core.theme_radar import THEME_ALIASES, normalize_theme_name, summarize_theme_radar
from utils.safe import drop_empty as _drop_empty_candidate_fields

logger = logging.getLogger(__name__)

_VALID_BOARDS = {"all", "main", "chinext", "star", "bse", "main_chinext_star"}
_MAX_SCAN_LIMIT = 3000
_AGENT_DEFAULT_SCAN_LIMIT = 1200
_BOARD_ALIAS = {
    "gem": "chinext",
    "创业板": "chinext",
    "主板": "main",
    "科创板": "star",
    "科创": "star",
    "北交所": "bse",
    "北交": "bse",
    "star": "star",
    "bse": "bse",
    "全部": "all",
    "主板+创业板": "main_chinext_star",
    "主板和创业板": "main_chinext_star",
    "主板创业板科创": "main_chinext_star",
    "主板创业板科创板": "main_chinext_star",
    "主板+创业板+科创": "main_chinext_star",
    "主板+创业板+科创板": "main_chinext_star",
    "主板和创业板和科创": "main_chinext_star",
    "主板和创业板和科创板": "main_chinext_star",
    "主板+科创板": "main_chinext_star",
    "主板和科创板": "main_chinext_star",
    "主板+科创": "main_chinext_star",
    "主板和科创": "main_chinext_star",
    "创业板+科创板": "main_chinext_star",
    "创业板和科创板": "main_chinext_star",
    "创业板+科创": "main_chinext_star",
    "创业板和科创": "main_chinext_star",
    "沪深a": "main_chinext_star",
    "沪深 a": "main_chinext_star",
    "沪深a股": "main_chinext_star",
    "沪深 a股": "main_chinext_star",
    "沪深a 股": "main_chinext_star",
    "不含北交": "main_chinext_star",
    "非北交": "main_chinext_star",
    "排除北交": "main_chinext_star",
    "剔除北交": "main_chinext_star",
    "双创": "main_chinext_star",
    "主创": "main_chinext_star",
    "main_chinext": "main_chinext_star",
    "main-chinext": "main_chinext_star",
    "main+chinext": "main_chinext_star",
}


def screen_stocks(
    board: str = "all",
    limit: int | None = None,
    style: str | list[str] | None = None,
    theme: str | None = None,
    financial_metrics: bool | str | None = None,
    tool_context: ToolContext | None = None,
) -> dict:
    """运行 Wyckoff 五层漏斗筛选。"""
    try:
        ensure_tushare_token(tool_context)
        board = _normalize_board(board)
        if board not in _VALID_BOARDS:
            return {"error": f"不支持的 board 值 '{board}'，可选: all / main / chinext / star / bse"}
        pool_limit = _normalize_scan_limit(limit, tool_context=tool_context)
        include_financial_metrics = _resolve_financial_metrics_mode(
            financial_metrics,
            pool_limit=pool_limit,
            tool_context=tool_context,
        )
        ok, symbols, _bench_ctx, details = _run_funnel_with_board(
            board,
            pool_limit=pool_limit,
            include_financial_metrics=include_financial_metrics,
        )
        result = _build_screen_result(
            ok=ok,
            board=board,
            style=style,
            theme=theme,
            include_financial_metrics=include_financial_metrics,
            symbols=symbols,
            details=details,
        )
        remember_screen_handoff(tool_context, result)
        return result
    except Exception as e:
        logger.exception("screen_stocks error")
        return {"error": str(e)}


def _build_screen_result(
    *,
    ok: bool,
    board: str,
    style: str | list[str] | None,
    theme: str | None,
    include_financial_metrics: bool,
    symbols: list[Any],
    details: dict,
) -> dict[str, Any]:
    metrics = details.get("metrics") or {}
    trigger_groups = _trigger_summary(details)
    trade_mode = _trade_mode_summary(details)
    theme_context = _theme_context(metrics)
    style_preference, theme_preference, top_candidates = _preferred_ranked_candidates(
        style, theme, trigger_groups, symbols, details
    )
    preference_match = _preference_match_summary(style_preference, theme_preference, top_candidates)
    top_candidates = _annotate_preference_miss_risks(
        top_candidates,
        style_preference,
        theme_preference,
    )
    summary = _screen_summary(metrics, symbols)
    data_quality = _data_quality_summary(metrics, summary)
    decision_brief = _decision_brief(trade_mode, top_candidates, data_quality)
    selection_brief = _selection_brief(
        trade_mode,
        top_candidates,
        data_quality,
        style_preference,
        theme_preference,
    )
    action_plan = _action_plan(trade_mode, top_candidates, data_quality)
    top_candidates = _annotate_top_candidate_actions(top_candidates, action_plan)
    guard_summary = _screen_candidate_guard_summary(selection_brief, action_plan)
    decision_state = _screen_decision_state(selection_brief, action_plan, trade_mode, guard_summary)
    symbols_for_report = list(action_plan.get("report_candidates") or [])
    watch_candidates = list(action_plan.get("watch_candidates") or [])
    diagnosis_targets = list(action_plan.get("diagnosis_targets") or [])
    summary["report_candidates"] = len(_report_rows(symbols_for_report))
    summary["watch_candidates"] = len(watch_candidates)
    next_tool = _screen_next_tool(selection_brief, action_plan)
    result = _screen_result_payload(
        ok=ok,
        board=board,
        style_preference=style_preference,
        theme_preference=theme_preference,
        preference_match=preference_match,
        include_financial_metrics=include_financial_metrics,
        metrics=metrics,
        trigger_groups=trigger_groups,
        summary=summary,
        data_quality=data_quality,
        strategy_policy=_strategy_policy_summary(details),
        trade_mode=trade_mode,
        decision_brief=decision_brief,
        selection_brief=selection_brief,
        decision_state=decision_state,
        theme_context=theme_context,
        action_plan=action_plan,
        next_tool=next_tool,
        top_candidates=top_candidates,
        symbols_for_report=symbols_for_report,
        watch_candidates=watch_candidates,
        diagnosis_targets=diagnosis_targets,
    )
    if guard_summary:
        result["candidate_guard_summary"] = guard_summary
    return result


def _screen_result_payload(**payload: Any) -> dict[str, Any]:
    summary = payload["summary"]
    action_plan = payload["action_plan"]
    symbols_for_report = payload["symbols_for_report"]
    return {
        "ok": bool(payload["ok"]),
        "board": payload["board"],
        "style_preference": payload["style_preference"],
        "theme_preference": payload["theme_preference"],
        "preference_match": payload["preference_match"],
        "scan_scope": _scan_scope(
            payload["board"],
            summary,
            payload["metrics"],
            payload["include_financial_metrics"],
            payload["style_preference"],
            payload["theme_preference"],
            payload["preference_match"],
        ),
        "summary": summary,
        "data_quality": payload["data_quality"],
        "strategy_policy": payload["strategy_policy"],
        "trade_mode": payload["trade_mode"],
        "decision_brief": payload["decision_brief"],
        "selection_brief": payload["selection_brief"],
        "decision_state": payload["decision_state"],
        "theme_context": payload["theme_context"],
        "action_plan": action_plan,
        "next_action": _screen_next_action(payload["selection_brief"], action_plan, payload["next_tool"]),
        "next_tool": payload["next_tool"],
        "top_candidates": payload["top_candidates"],
        "trigger_groups": payload["trigger_groups"],
        "top_sectors": payload["metrics"].get("top_sectors", []),
        "etf_enhancement": payload["metrics"].get("etf_enhancement", {}),
        "etf_candidates": payload["metrics"].get("etf_candidates", []),
        "symbols_for_report": symbols_for_report,
        "report_candidates": symbols_for_report,
        "watch_candidates": payload["watch_candidates"],
        "diagnosis_targets": payload["diagnosis_targets"],
        "quality_gate": action_plan.get("quality_gate", {}),
    }


def _normalize_board(board: str) -> str:
    board = str(board or "all").strip().lower()
    return _BOARD_ALIAS.get(board, board)


def _preferred_ranked_candidates(
    style: str | list[str] | None,
    theme: str | None,
    trigger_groups: dict,
    symbols: list[Any],
    details: dict,
) -> tuple[dict[str, Any], dict[str, Any], list[dict]]:
    style_preference = _normalize_style_preference(style)
    theme_preference = _normalize_theme_preference(theme)
    candidates = _ranked_candidates(trigger_groups, symbols, details.get("name_map") or {}, details)
    candidates = _apply_style_preference(candidates, style_preference)
    candidates = _apply_theme_preference(candidates, theme_preference)
    return style_preference, theme_preference, candidates


_STYLE_ALIASES = {
    "trend": (
        "trend",
        "strong",
        "right",
        "趋势",
        "强势",
        "右侧",
        "突破",
        "主升",
        "最强",
        "领涨",
        "龙头",
        "强度",
        "短线",
        "起爆",
        "弹性",
        "小盘弹性",
        "刚启动",
        "初启动",
        "启动初期",
    ),
    "pullback": (
        "pullback",
        "accum",
        "left",
        "低吸",
        "吸筹",
        "左侧",
        "回踩",
        "回调",
        "埋伏",
        "低位",
        "别追高",
        "不追高",
        "不要追高",
        "别追涨",
        "不追涨",
        "不要追涨",
        "避开高位",
        "不要高位",
        "非高位",
        "不高位",
        "没涨太多",
        "涨幅不大",
        "位置不高",
    ),
    "quality": (
        "quality",
        "stable",
        "balanced",
        "稳健",
        "高质量",
        "质量",
        "安全",
        "性价比",
        "赔率",
        "盈亏比",
        "成交活跃",
        "流动性好",
        "高流动性",
        "成交额大",
        "换手充分",
        "白马",
    ),
}

_STYLE_TEXT_COMPACT_RE = re.compile(r"[\s。！!,.，、？?；;：:（）()【】\[\]\"'`]+")
_STYLE_TEXT_REPLACEMENTS = (
    ("强事", "强势"),
    ("趋式", "趋势"),
    ("底吸", "低吸"),
    ("底位", "低位"),
)


def _normalize_style_preference(value: str | list[str] | None) -> dict[str, Any]:
    raw_items = value if isinstance(value, list) else [value]
    raw_text = " ".join(str(item or "") for item in raw_items).strip().lower()
    if not raw_text:
        return {}
    match_text = _normalize_style_match_text(raw_text)
    styles = [name for name, aliases in _STYLE_ALIASES.items() if any(alias.lower() in match_text for alias in aliases)]
    return _drop_empty_candidate_fields({"raw": raw_text[:80], "styles": list(dict.fromkeys(styles))})


def _normalize_style_match_text(text: str) -> str:
    normalized = text
    for source, target in _STYLE_TEXT_REPLACEMENTS:
        normalized = normalized.replace(source, target)
    return _STYLE_TEXT_COMPACT_RE.sub("", normalized)


def _normalize_theme_preference(value: str | None) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    theme = normalize_theme_name(text) or text
    return _drop_empty_candidate_fields({"raw": text[:80], "theme": theme[:40]})


def _normalize_scan_limit(limit: int | None, *, tool_context: ToolContext | None = None) -> int | None:
    if limit in (None, ""):
        return _agent_default_scan_limit() if tool_context is not None else None
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是非负整数；limit=0 表示全量扫描") from None
    if value < 0:
        raise ValueError("limit 必须是非负整数；limit=0 表示全量扫描")
    if value > _MAX_SCAN_LIMIT:
        raise ValueError(f"limit 最大支持 {_MAX_SCAN_LIMIT}；全量扫描请传 limit=0")
    return value


def _agent_default_scan_limit() -> int:
    raw = str(os.getenv("WYCKOFF_AGENT_SCREEN_DEFAULT_LIMIT", _AGENT_DEFAULT_SCAN_LIMIT) or "").strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return _AGENT_DEFAULT_SCAN_LIMIT
    return max(min(value, _MAX_SCAN_LIMIT), 0)


def _resolve_financial_metrics_mode(
    value: bool | str | None,
    *,
    pool_limit: int | None,
    tool_context: ToolContext | None,
) -> bool:
    explicit = _coerce_optional_bool(value)
    if explicit is not None:
        return explicit
    env_value = _coerce_optional_bool(os.getenv("WYCKOFF_AGENT_SCREEN_FINANCIAL_METRICS"))
    if env_value is not None and tool_context is not None:
        return env_value
    return not (tool_context is not None and pool_limit not in (None, 0))


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled", "include", "full"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "skip", "quick"}:
        return False
    return None


def remember_screen_handoff(tool_context: ToolContext | None, result: dict[str, Any]) -> None:
    if tool_context is None:
        return
    tool_context.state["last_screen_result"] = {
        "ok": result.get("ok"),
        "job_kind": result.get("job_kind"),
        "board": result.get("board"),
        "style_preference": result.get("style_preference", {}),
        "theme_preference": result.get("theme_preference", {}),
        "preference_match": result.get("preference_match", {}),
        "scan_scope": result.get("scan_scope", {}),
        "summary": result.get("summary", {}),
        "data_quality": result.get("data_quality", {}),
        "strategy_policy": result.get("strategy_policy", {}),
        "trade_mode": result.get("trade_mode", {}),
        "decision_brief": result.get("decision_brief", {}),
        "selection_brief": result.get("selection_brief", {}),
        "decision_state": result.get("decision_state", {}),
        "theme_context": result.get("theme_context", {}),
        "action_plan": result.get("action_plan", {}),
        "next_action": result.get("next_action", ""),
        "next_tool": result.get("next_tool", {}),
        "quality_gate": result.get("quality_gate", {}),
        "candidate_guard_summary": result.get("candidate_guard_summary", {}),
        "top_candidates": list(result.get("top_candidates") or [])[:20],
        "symbols_for_report": list(result.get("symbols_for_report") or [])[:10],
        "report_candidates": list(result.get("report_candidates") or [])[:10],
        "watch_candidates": list(result.get("watch_candidates") or [])[:10],
        "diagnosis_targets": list(result.get("diagnosis_targets") or [])[:5],
    }


def _run_funnel_with_board(board: str, *, pool_limit: int | None, include_financial_metrics: bool):
    from workflows.wyckoff_funnel import run as run_funnel

    return run_funnel(
        "",
        notify=False,
        return_details=True,
        pool_board=board,
        pool_limit_count=pool_limit,
        executor_mode="thread",
        include_financial_metrics=include_financial_metrics,
    )


def _trigger_summary(details: dict) -> dict:
    triggers = details.get("triggers") or {}
    name_map = details.get("name_map") or {}
    return {
        trigger_name: [
            {
                "code": str(code),
                "name": str(name_map.get(str(code), code)),
                "score": round(candidate_score_value(score), 2),
            }
            for code, score in rows
        ]
        for trigger_name, rows in triggers.items()
    }


def _screen_summary(metrics: dict, symbols_for_report: list[Any]) -> dict:
    return {
        "total_scanned": int(metrics.get("total_symbols", 0)),
        "scan_limit": int(metrics.get("pool_limit", 0) or 0),
        "layer1_passed": int(metrics.get("layer1", 0)),
        "layer2_passed": int(metrics.get("layer2", 0)),
        "layer3_passed": int(metrics.get("layer3", 0)),
        "report_candidates": len(_report_rows(symbols_for_report)),
    }


def _data_quality_summary(metrics: dict, summary: dict) -> dict:
    total = int(summary.get("total_scanned", 0) or 0)
    fetch_ok = int(metrics.get("fetch_ok", 0) or 0)
    fetch_fail = int(metrics.get("fetch_fail", 0) or 0)
    date_mismatch = int(metrics.get("fetch_date_mismatch", 0) or 0)
    spot_patched = int(metrics.get("fetch_spot_patched", 0) or 0)
    coverage_pct = round((fetch_ok / total) * 100, 1) if total > 0 else 0.0
    status = _data_quality_status(total, coverage_pct, date_mismatch)
    return {
        "status": status,
        "coverage_pct": coverage_pct,
        "fetch_ok": fetch_ok,
        "fetch_fail": fetch_fail,
        "date_mismatch": date_mismatch,
        "spot_patched": spot_patched,
        "end_trade_date": str(metrics.get("end_trade_date") or ""),
        "warnings": _data_quality_warnings(status, coverage_pct, fetch_fail, date_mismatch, spot_patched),
        "action": _data_quality_action(status),
    }


def _data_quality_status(total: int, coverage_pct: float, date_mismatch: int) -> str:
    if total <= 0:
        return "empty"
    if coverage_pct < 90.0 or date_mismatch > 0:
        return "degraded"
    if coverage_pct < 98.0:
        return "partial"
    return "ok"


def _data_quality_warnings(
    status: str,
    coverage_pct: float,
    fetch_fail: int,
    date_mismatch: int,
    spot_patched: int,
) -> list[str]:
    warnings: list[str] = []
    if status == "empty":
        warnings.append("本轮没有可用K线数据")
    if fetch_fail > 0:
        warnings.append(f"{fetch_fail}只股票拉取失败")
    if date_mismatch > 0:
        warnings.append(f"{date_mismatch}只股票交易日不匹配")
    if spot_patched > 0:
        warnings.append(f"{spot_patched}只股票使用实时快照补齐")
    if status in {"partial", "degraded"} and coverage_pct > 0:
        warnings.append(f"数据覆盖率 {coverage_pct:.1f}%")
    return warnings


def _data_quality_action(status: str) -> str:
    return {
        "ok": "可正常参考候选排序",
        "partial": "候选可参考，但需要优先复核缺失数据影响",
        "degraded": "不要直接据此选股，先重跑或缩小扫描范围",
        "empty": "无法形成可靠候选，需先修复数据源",
    }.get(status, "需要复核数据质量")


def _data_quality_blocks_review(data_quality: dict | None) -> bool:
    status = str((data_quality or {}).get("status") or "")
    return status in {"degraded", "empty"}


def _data_quality_blocks_ready_flow(trade_mode: dict, data_quality: dict | None) -> bool:
    if not bool(trade_mode.get("allow_ai_review") or trade_mode.get("allow_recommendation_write")):
        return False
    return _data_quality_blocks_review(data_quality)


def _data_quality_gate(trade_mode: dict, data_quality: dict | None) -> dict:
    if not _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return {}
    return {
        "status": str((data_quality or {}).get("status") or "degraded"),
        "reason": _data_quality_gate_reason(data_quality),
        "warnings": list((data_quality or {}).get("warnings") or []),
    }


def _data_quality_gate_reason(data_quality: dict | None) -> str:
    payload = data_quality or {}
    action = str(payload.get("action") or "数据质量不足，先重跑或缩小扫描范围").strip()
    warnings = [str(item) for item in payload.get("warnings") or [] if str(item).strip()]
    return "；".join([action, *warnings[:3]])


def _data_quality_risk_factors(data_quality: dict | None) -> list[str]:
    if not _data_quality_blocks_review(data_quality):
        return []
    payload = data_quality or {}
    factors = [str(payload.get("action") or "数据质量不足")]
    factors.extend(str(item) for item in payload.get("warnings") or [])
    return list(dict.fromkeys(factor for factor in factors if factor))


def _scan_scope(
    board: str,
    summary: dict,
    metrics: dict,
    include_financial_metrics: bool,
    style_preference: dict[str, Any] | None = None,
    theme_preference: dict[str, Any] | None = None,
    preference_match: dict[str, Any] | None = None,
) -> dict:
    limit = int(metrics.get("pool_limit", 0) or 0)
    scope = "bounded" if limit > 0 else "full"
    payload = {
        "scope": scope,
        "board": board,
        "limit": limit,
        "total_scanned": int(summary.get("total_scanned", 0) or 0),
        "financial_metrics": _financial_metrics_scope(metrics, include_financial_metrics),
        "financial_metrics_count": int(metrics.get("financial_metrics_count", 0) or 0),
    }
    if style_preference:
        payload["style_preference"] = style_preference
    if theme_preference:
        payload["theme_preference"] = theme_preference
    if preference_match:
        payload["preference_match"] = preference_match
    return payload


def _financial_metrics_scope(metrics: dict, include_financial_metrics: bool) -> str:
    if not include_financial_metrics:
        return "skipped_quick_scan"
    return "available" if int(metrics.get("financial_metrics_count", 0) or 0) > 0 else "requested_unavailable"


def _trade_mode_summary(details: dict) -> dict:
    mode = details.get("trade_mode") if isinstance(details, dict) else {}
    if not isinstance(mode, dict):
        return {}
    fields = (
        "regime",
        "mode",
        "label",
        "action",
        "reason",
        "allow_ai_review",
        "allow_recommendation_write",
    )
    return {field: mode[field] for field in fields if field in mode}


def _strategy_policy_summary(details: dict) -> dict:
    policy = details.get("strategy_policy") if isinstance(details, dict) else {}
    if not isinstance(policy, dict):
        return {}
    fields = (
        "dynamic_mode",
        "signal_weights",
        "attribution_signal_weights",
        "selection_action_count",
        "selection_action_summary",
        "formal_dynamic_allowed",
        "policy_weight_active_scope",
        "execution_policy",
        "next_action",
    )
    payload = {field: policy[field] for field in fields if field in policy}
    if mode := str(policy.get("dynamic_mode") or "").strip():
        payload["dynamic_mode_label"] = policy_execution_mode_label(mode)
    if mode := str(policy.get("execution_policy") or "").strip():
        payload["execution_policy_label"] = policy_execution_mode_label(mode)
    if action := str(policy.get("next_action") or "").strip():
        payload["next_action_label"] = policy_next_action_label(action)
    return payload


def _theme_context(metrics: dict) -> dict[str, Any]:
    radar = metrics.get("theme_radar") or metrics.get("theme_radar_current") or {}
    return _drop_empty_candidate_fields(
        {
            "today_activity": str(metrics.get("theme_activity_summary") or "").strip(),
            "event_mainlines": str(metrics.get("ths_hot_events_summary") or "").strip(),
            "theme_radar": summarize_theme_radar(radar),
            "theme_radar_source": str(metrics.get("theme_radar_source") or "").strip(),
            "hot_concepts": _theme_context_list(metrics.get("theme_lines"), 6),
        }
    )


def _theme_context_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value[:limit] if str(item).strip()]


def _action_plan(trade_mode: dict, top_candidates: list[dict], data_quality: dict) -> dict:
    report_candidates = _report_candidates(top_candidates)
    watch_candidates = _watch_candidates(top_candidates)
    gate = _data_quality_gate(trade_mode, data_quality)
    quality_gate = _quality_gate(top_candidates)
    trade_readiness = _screen_trade_readiness(report_candidates, trade_mode, data_quality, quality_gate)
    diagnosis_targets = _diagnosis_targets(report_candidates, watch_candidates, trade_mode, data_quality)
    payload = {
        "primary_action": str(trade_mode.get("action") or _candidate_action_label(trade_mode)),
        "candidate_action": _candidate_action_label(trade_mode),
        "new_buy_allowed": bool(report_candidates) and bool(trade_mode.get("allow_recommendation_write")) and not gate,
        "ai_review_allowed": bool(report_candidates) and bool(trade_mode.get("allow_ai_review")) and not gate,
        "trade_readiness": trade_readiness,
        "review_targets": _review_targets(report_candidates, trade_mode, data_quality, quality_gate),
        "report_candidates": _candidate_refs(report_candidates, trade_mode, "report", data_quality),
        "watch_candidates": _candidate_refs(watch_candidates, trade_mode, "watch", data_quality),
    }
    if diagnosis_targets:
        payload["diagnosis_targets"] = diagnosis_targets
    if gate:
        payload["data_quality_gate"] = gate
    if quality_gate:
        payload["quality_gate"] = quality_gate
    return payload


def _screen_trade_readiness(
    report_candidates: list[dict],
    trade_mode: dict,
    data_quality: dict,
    quality_gate: dict[str, Any],
) -> str:
    if _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return "data_quality_blocked"
    if quality_gate and not report_candidates:
        return "quality_blocked"
    if not report_candidates:
        return "watch_only"
    if not bool(trade_mode.get("allow_ai_review")):
        return "observe_only"
    if not bool(trade_mode.get("allow_recommendation_write")):
        return "research_only"
    if str(trade_mode.get("mode") or "").strip() == "confirmation_only":
        return "confirmation_required"
    if str(trade_mode.get("mode") or "").strip() == "repair_probe":
        return "probe_ready"
    return "review_ready"


def _screen_decision_state(
    selection_brief: dict,
    action_plan: dict,
    trade_mode: dict,
    candidate_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primary = selection_brief.get("primary_pick") if isinstance(selection_brief.get("primary_pick"), dict) else {}
    trade_readiness = str(action_plan.get("trade_readiness") or "").strip()
    new_buy_allowed = bool(action_plan.get("new_buy_allowed"))
    ai_review_allowed = bool(action_plan.get("ai_review_allowed"))
    guard_reason = _primary_candidate_guard_reason(primary, candidate_guard or {})
    reason = _screen_decision_reason(action_plan, trade_mode)
    next_step = str(primary.get("next_step") or action_plan.get("candidate_action") or "").strip()
    payload = {
        "status": str(selection_brief.get("status") or "").strip(),
        "label": _screen_decision_label(primary, trade_readiness, new_buy_allowed, ai_review_allowed),
        "trade_readiness": trade_readiness,
        "new_buy_allowed": new_buy_allowed,
        "candidate_direct_buy_allowed": bool(primary) and new_buy_allowed and not guard_reason,
        "candidate_guard_reason": guard_reason,
        "ai_review_allowed": ai_review_allowed,
        "primary": _screen_decision_primary(primary),
        "reason": reason,
        "next_step": next_step,
    }
    payload["summary"] = _screen_decision_summary(payload)
    return _drop_empty_candidate_fields(payload)


def _primary_candidate_guard_reason(primary: dict, candidate_guard: dict[str, Any]) -> str:
    code = str(primary.get("code") or "").strip()
    candidates = candidate_guard.get("candidates") if isinstance(candidate_guard, dict) else []
    if not code or not isinstance(candidates, list):
        return ""
    for row in candidates:
        if not isinstance(row, dict) or str(row.get("code") or "").strip() != code:
            continue
        return str(row.get("reason") or "").strip()
    return ""


def _screen_decision_reason(action_plan: dict, trade_mode: dict) -> str:
    review = action_plan.get("review_targets") if isinstance(action_plan.get("review_targets"), dict) else {}
    data_gate = action_plan.get("data_quality_gate") if isinstance(action_plan.get("data_quality_gate"), dict) else {}
    quality_gate = action_plan.get("quality_gate") if isinstance(action_plan.get("quality_gate"), dict) else {}
    trade_reason = str(trade_mode.get("reason") or "").strip()
    if str(review.get("status") or "").strip() == "empty" and trade_reason:
        return trade_reason
    return str(
        review.get("reason") or data_gate.get("reason") or quality_gate.get("reason") or trade_reason or ""
    ).strip()


def _screen_decision_label(
    primary: dict,
    trade_readiness: str,
    new_buy_allowed: bool,
    ai_review_allowed: bool,
) -> str:
    if not primary:
        return "无候选"
    if trade_readiness == "data_quality_blocked":
        return "数据质量阻断"
    if trade_readiness == "quality_blocked":
        return "质量门槛阻断"
    if new_buy_allowed:
        return "AI复核候选"
    if ai_review_allowed:
        return "仅研究复核"
    if trade_readiness == "observe_only":
        return "好股观察"
    return "观察候选"


def _screen_decision_primary(primary: dict) -> str:
    code = str(primary.get("code") or "").strip()
    name = str(primary.get("name") or "").strip()
    return " ".join(part for part in (code, name) if part)


def _screen_decision_summary(payload: dict[str, Any]) -> str:
    parts = [
        f"筛股决策: {payload.get('label')}",
        f"首选: {payload.get('primary')}" if payload.get("primary") else "",
        f"市场新增: {'开' if payload.get('new_buy_allowed') else '关'}",
        _candidate_direct_buy_summary(payload),
        f"AI复核: {'可' if payload.get('ai_review_allowed') else '不可'}",
        f"原因: {payload.get('reason')}" if payload.get("reason") else "",
        f"下一步: {payload.get('next_step')}" if payload.get("next_step") else "",
    ]
    return " · ".join(part for part in parts if part)


def _candidate_direct_buy_summary(payload: dict[str, Any]) -> str:
    if not payload.get("primary"):
        return ""
    if payload.get("candidate_direct_buy_allowed"):
        return "候选直买: 可"
    return "候选直买: 禁" if payload.get("candidate_guard_reason") else "候选直买: 关"


def _decision_brief(trade_mode: dict, top_candidates: list[dict], data_quality: dict) -> dict:
    report_candidates = _report_candidates(top_candidates)
    watch_candidates = _watch_candidates(top_candidates)
    gate = _data_quality_gate(trade_mode, data_quality)
    return {
        "market_gate": _market_gate_line(trade_mode),
        "next_action": gate.get("reason") or _candidate_action_label(trade_mode),
        "report_focus": _candidate_brief_items(report_candidates, trade_mode, "report", data_quality),
        "watch_focus": _candidate_brief_items(watch_candidates, trade_mode, "watch", data_quality),
    }


def _selection_brief(
    trade_mode: dict,
    top_candidates: list[dict],
    data_quality: dict,
    style_preference: dict[str, Any] | None = None,
    theme_preference: dict[str, Any] | None = None,
) -> dict:
    report_candidates = _report_candidates(top_candidates)
    candidates = report_candidates or top_candidates[:3]
    status = _selection_status(report_candidates, candidates, trade_mode, data_quality)
    best_candidates = _selection_candidate_items(
        candidates,
        trade_mode,
        "report" if report_candidates else "watch",
        data_quality,
    )
    payload = {
        "status": status,
        "headline": _selection_headline(status, best_candidates),
        "best_codes": [row["code"] for row in best_candidates],
        "primary_pick": best_candidates[0] if best_candidates else {},
        "best_candidates": best_candidates,
        "preference_alternatives": _selection_preference_alternatives(
            top_candidates,
            best_candidates,
            trade_mode,
            data_quality,
            style_preference or {},
            theme_preference or {},
        ),
        "tool_handoff": _selection_tool_handoff(status, best_candidates),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _selection_status(
    report_candidates: list[dict], candidates: list[dict], trade_mode: dict, data_quality: dict
) -> str:
    if not candidates:
        return "empty"
    if report_candidates and _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return "blocked_by_data_quality"
    if report_candidates and bool(trade_mode.get("allow_ai_review")):
        return "ready_for_ai_review"
    if report_candidates:
        return "blocked_by_market_gate"
    return "watch_only"


def _selection_candidate_items(
    candidates: list[dict],
    trade_mode: dict,
    bucket: str,
    data_quality: dict,
    *,
    limit: int = 5,
) -> list[dict]:
    return [_selection_candidate_item(row, trade_mode, bucket, data_quality) for row in candidates[:limit]]


def _selection_preference_alternatives(
    top_candidates: list[dict],
    best_candidates: list[dict],
    trade_mode: dict,
    data_quality: dict,
    style_preference: dict[str, Any],
    theme_preference: dict[str, Any],
    *,
    limit: int = 3,
) -> list[dict]:
    if not _has_style_preference(style_preference) and not _has_theme_preference(theme_preference):
        return []
    best_codes = {str(row.get("code") or "").strip() for row in best_candidates}
    alternatives: list[dict] = []
    for row in top_candidates:
        code = str(row.get("code") or "").strip()
        if (
            not code
            or code in best_codes
            or not _candidate_satisfies_preferences(row, style_preference, theme_preference)
        ):
            continue
        bucket = "report" if row.get("selected_for_report") else "watch"
        alternatives.append(_selection_candidate_item(row, trade_mode, bucket, data_quality))
        if len(alternatives) >= limit:
            return alternatives
    return alternatives


def _candidate_satisfies_preferences(
    row: dict,
    style_preference: dict[str, Any],
    theme_preference: dict[str, Any],
) -> bool:
    if _candidate_missing_style_preference_labels(row, style_preference):
        return False
    if _has_theme_preference(theme_preference) and not _candidate_matches_preference(row, "theme"):
        return False
    return _has_style_preference(style_preference) or _has_theme_preference(theme_preference)


def _selection_candidate_item(row: dict, trade_mode: dict, bucket: str, data_quality: dict) -> dict:
    profile = _candidate_profile(row)
    rank_reason = str(row.get("rank_reason") or "").strip()
    why = "；".join(part for part in (profile, rank_reason) if part) or "候选证据不足"
    next_step = _candidate_next_step(trade_mode, bucket, data_quality)
    action_fields = _candidate_action_fields(trade_mode, bucket, data_quality)
    return _drop_empty_candidate_fields(
        {
            "code": str(row.get("code") or "").strip(),
            "name": str(row.get("name") or row.get("code") or "").strip(),
            "tier": _candidate_quality_label(row),
            "why": why,
            "quality_factors": _candidate_quality_factors(row, next_step=next_step),
            "risk_factors": _candidate_risk_factors(row, trade_mode, bucket, data_quality),
            **action_fields,
            "next_step": next_step,
            "priority_score": row.get("priority_score"),
            "shadow_score": row.get("shadow_score"),
            "score": row.get("score"),
            "track": row.get("track"),
            "stage": row.get("stage"),
            "candidate_lane": row.get("candidate_lane"),
            "entry_type": row.get("entry_type"),
            **_candidate_style_fields(row),
            **_candidate_theme_result_fields(row),
            **_candidate_quality_metrics(row),
        }
    )


def _selection_headline(status: str, best_candidates: list[dict]) -> str:
    if not best_candidates:
        return "本轮没有形成可复核候选"
    first = best_candidates[0]
    prefix = {
        "ready_for_ai_review": "本轮首选可进入 AI 研报复核",
        "blocked_by_data_quality": "本轮有候选，但数据质量未过关",
        "blocked_by_market_gate": "本轮有强候选，但市场闸门未打开",
        "watch_only": "本轮只有观察候选",
    }.get(status, "本轮候选摘要")
    return f"{prefix}: {first.get('code')} {first.get('name')}"


def _selection_tool_handoff(status: str, best_candidates: list[dict]) -> dict:
    if status != "ready_for_ai_review":
        return {}
    codes = [str(row.get("code") or "").strip() for row in best_candidates if str(row.get("code") or "").strip()]
    return {
        "tool": "generate_ai_report",
        "args": {"stock_codes": codes[:10]},
        "reason": "首选候选已通过市场闸门，可进入 AI 研报复核",
    }


def _screen_next_tool(selection_brief: dict, action_plan: dict) -> dict:
    selection_tool = selection_brief.get("tool_handoff") if isinstance(selection_brief, dict) else {}
    if tool := _compact_next_tool(selection_tool):
        return tool
    review = action_plan.get("review_targets") if isinstance(action_plan, dict) else {}
    if tool := _compact_next_tool(review):
        return tool
    targets = action_plan.get("diagnosis_targets") if isinstance(action_plan, dict) else []
    if isinstance(targets, list) and targets:
        return _compact_next_tool(targets[0])
    return {}


def _compact_next_tool(payload: Any) -> dict:
    if not isinstance(payload, dict) or not payload.get("tool"):
        return {}
    out = {"tool": str(payload.get("tool") or "")}
    args = payload.get("args")
    if isinstance(args, dict) and args:
        out["args"] = args
    if reason := str(payload.get("reason") or "").strip():
        out["reason"] = reason
    return out


def _diagnosis_targets(
    report_candidates: list[dict],
    watch_candidates: list[dict],
    trade_mode: dict,
    data_quality: dict,
    *,
    limit: int = 3,
) -> list[dict]:
    if _data_quality_blocks_review(data_quality):
        return []
    bucket = "report" if report_candidates else "watch"
    candidates = report_candidates or watch_candidates
    return [
        _diagnosis_target_from_ref(_candidate_ref(row, trade_mode, bucket, data_quality), bucket)
        for row in candidates[:limit]
        if str(row.get("code") or "").strip()
    ]


def _diagnosis_target_from_ref(ref: dict, bucket: str) -> dict:
    code = str(ref.get("code") or "").strip()
    next_step = "诊断个股结构，确认触发位和失效位"
    reason = "研报候选先做个股结构复核" if bucket == "report" else "观察候选先做个股结构诊断"
    return _drop_empty_candidate_fields(
        {
            "tool": "analyze_stock",
            "args": {"code": code, "mode": "diagnose"},
            "code": code,
            "name": ref.get("name"),
            "reason": reason,
            "action_status": ref.get("action_status"),
            "next_step": next_step,
            "risk_factors": list(ref.get("risk_factors") or [])[:3],
        }
    )


def _screen_next_action(selection_brief: dict, action_plan: dict, next_tool: dict) -> str:
    if next_tool:
        return str(next_tool.get("reason") or "调用下一步工具复核候选")
    review = action_plan.get("review_targets") if isinstance(action_plan, dict) else {}
    reason = str(review.get("reason") or "").strip() if isinstance(review, dict) else ""
    status = (
        str(review.get("status") or selection_brief.get("status") or "").strip() if isinstance(review, dict) else ""
    )
    if status == "blocked_by_data_quality":
        return f"先处理数据质量阻断: {reason}" if reason else "先处理数据质量阻断"
    if status == "blocked_by_quality_gate":
        return f"保留观察池，暂不生成 AI 研报: {reason}" if reason else "保留观察池，暂不生成 AI 研报"
    if status in {"blocked", "blocked_by_market_gate"}:
        return f"市场闸门未打开，先观察候选: {reason}" if reason else "市场闸门未打开，先观察候选"
    if status == "watch_only":
        return "本轮只有观察候选，等待形成研报候选后再继续"
    return str(selection_brief.get("headline") or action_plan.get("candidate_action") or "继续复核筛股结果")


def _screen_candidate_guard_summary(selection_brief: dict, action_plan: dict) -> dict[str, Any]:
    rows: list[dict] = []
    for key in ("best_candidates",):
        value = selection_brief.get(key) if isinstance(selection_brief, dict) else []
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    if isinstance(action_plan, dict):
        for key in ("report_candidates", "watch_candidates"):
            value = action_plan.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
    return candidate_guard_summary(_dedupe_guard_rows(rows))


def _annotate_top_candidate_actions(candidates: list[dict], action_plan: dict) -> list[dict]:
    action_rows = _candidate_action_rows(action_plan)
    out: list[dict] = []
    for row in candidates:
        code = str(row.get("code") or "").strip()
        action_row = action_rows.get(code, {})
        out.append(_annotate_top_candidate_action(row, action_row))
    return out


def _candidate_action_rows(action_plan: dict) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for key in ("report_candidates", "watch_candidates"):
        value = action_plan.get(key) if isinstance(action_plan, dict) else []
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code:
                rows[code] = {**row, "_final_bucket": "report" if key == "report_candidates" else "watch"}
    return rows


def _annotate_top_candidate_action(row: dict, action_row: dict) -> dict:
    if not action_row:
        return dict(row)
    payload = dict(row)
    for field in ("action_status", "next_step"):
        value = action_row.get(field)
        if value not in (None, "", [], {}):
            payload[field] = value
    final_selected = action_row.get("_final_bucket") == "report"
    if payload.get("selected_for_report") != final_selected:
        payload["raw_selected_for_report"] = payload.get("selected_for_report")
    payload["selected_for_report"] = final_selected
    payload["risk_factors"] = _merge_candidate_factors(row.get("risk_factors"), action_row.get("risk_factors"))
    return payload


def _merge_candidate_factors(*values: Any) -> list[str]:
    factors: list[str] = []
    for value in values:
        if isinstance(value, list):
            factors.extend(str(item).strip() for item in value if str(item).strip())
        elif str(value or "").strip():
            factors.append(str(value).strip())
    return list(dict.fromkeys(factors))


def _dedupe_guard_rows(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        out.setdefault(code, row)
    return list(out.values())


def _report_candidates(top_candidates: list[dict]) -> list[dict]:
    return list(split_ai_review_candidates(top_candidates).get("report_candidates") or [])


def _watch_candidates(top_candidates: list[dict]) -> list[dict]:
    return list(split_ai_review_candidates(top_candidates).get("watch_candidates") or [])


def _quality_gate(top_candidates: list[dict]) -> dict[str, Any]:
    return dict(split_ai_review_candidates(top_candidates).get("quality_gate") or {})


def _review_targets(
    report_candidates: list[dict],
    trade_mode: dict,
    data_quality: dict,
    quality_gate: dict[str, Any] | None = None,
) -> dict:
    codes = [str(row.get("code") or "").strip() for row in report_candidates if str(row.get("code") or "").strip()]
    payload = {
        "codes": codes[:10],
        "status": _review_target_status(codes, trade_mode, data_quality, quality_gate or {}),
        "reason": _review_target_reason(codes, trade_mode, data_quality, quality_gate or {}),
    }
    if payload["status"] == "ready":
        payload["tool"] = "generate_ai_report"
        payload["args"] = {"stock_codes": payload["codes"]}
    return payload


def _review_target_status(
    codes: list[str],
    trade_mode: dict,
    data_quality: dict,
    quality_gate: dict[str, Any],
) -> str:
    if not codes:
        if quality_gate:
            return "blocked_by_quality_gate"
        return "empty"
    if not bool(trade_mode.get("allow_ai_review")):
        return "blocked"
    if _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return "blocked_by_data_quality"
    return "ready"


def _review_target_reason(
    codes: list[str],
    trade_mode: dict,
    data_quality: dict,
    quality_gate: dict[str, Any],
) -> str:
    if not codes:
        if quality_gate:
            return str(quality_gate.get("reason") or "候选风险调整质量分低于AI复核门槛")
        return "本轮没有研报候选"
    if not bool(trade_mode.get("allow_ai_review")):
        return str(trade_mode.get("reason") or "市场风险闸门未打开，暂不进入 AI 研报复核")
    if _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return _data_quality_gate_reason(data_quality)
    return "候选已可进入 AI 研报复核"


def _market_gate_line(trade_mode: dict) -> str:
    label = str(trade_mode.get("label") or trade_mode.get("regime") or "").strip()
    action = str(trade_mode.get("action") or _candidate_action_label(trade_mode)).strip()
    reason = str(trade_mode.get("reason") or "").strip()
    parts = [part for part in (label, action, reason) if part]
    return " / ".join(parts) or "市场闸门未提供"


def _candidate_action_label(trade_mode: dict) -> str:
    mode = str(trade_mode.get("mode") or "").strip()
    if mode == "observe_only":
        return "只观察，不新增买入"
    if mode == "repair_review":
        return "修复复核，暂不写正式推荐"
    if mode == "repair_probe":
        return "修复成立，只允许小额试探仓"
    if mode == "confirmation_only":
        return "等待二次确认后再行动"
    if mode == "mainline_active":
        return "主线/趋势可执行，优先确认买点"
    if mode == "overheat_shadow":
        return "过热市禁止新开：可做AI/shadow对照，不写正式推荐"
    if mode == "execution_blocked":
        return "该水温在下单闸门禁买：可做AI/shadow对照，不写正式推荐"
    if mode == "risk_on":
        return "过热市仅管理旧仓，不新开"
    return "先复核候选质量"


def _candidate_refs(
    candidates: list[dict],
    trade_mode: dict,
    bucket: str,
    data_quality: dict,
    *,
    limit: int = 5,
) -> list[dict]:
    return [_candidate_ref(row, trade_mode, bucket, data_quality) for row in candidates[:limit]]


def _candidate_brief_items(
    candidates: list[dict],
    trade_mode: dict,
    bucket: str,
    data_quality: dict,
    *,
    limit: int = 5,
) -> list[dict]:
    return [_candidate_brief_item(row, trade_mode, bucket, data_quality) for row in candidates[:limit]]


def _candidate_brief_item(row: dict, trade_mode: dict, bucket: str, data_quality: dict) -> dict:
    profile = _candidate_profile(row)
    rank_reason = str(row.get("rank_reason") or "").strip()
    next_step = _candidate_next_step(trade_mode, bucket, data_quality)
    evidence = "；".join(part for part in (profile, rank_reason) if part) or "候选证据不足"
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or code).strip()
    action_fields = _candidate_action_fields(trade_mode, bucket, data_quality)
    return {
        "code": code,
        "name": name,
        "quality": _candidate_quality_label(row),
        "evidence": evidence,
        "quality_factors": _candidate_quality_factors(row, next_step=next_step),
        "risk_factors": _candidate_risk_factors(row, trade_mode, bucket, data_quality),
        **action_fields,
        "next_step": next_step,
        **_candidate_style_fields(row),
        **_candidate_quality_metrics(row),
        "summary": f"{code} {name}: {evidence}；{next_step}",
    }


def _candidate_ref(row: dict, trade_mode: dict, bucket: str, data_quality: dict) -> dict:
    next_step = _candidate_next_step(trade_mode, bucket, data_quality)
    action_fields = _candidate_action_fields(trade_mode, bucket, data_quality)
    payload = {
        "code": row.get("code"),
        "name": row.get("name"),
        "quality": _candidate_quality_label(row),
        "profile": _candidate_profile(row),
        "next_step": next_step,
        "rank_reason": row.get("rank_reason"),
        "quality_factors": _candidate_quality_factors(row, next_step=next_step),
        "risk_factors": _candidate_risk_factors(row, trade_mode, bucket, data_quality),
        **action_fields,
        "priority_score": row.get("priority_score"),
        "shadow_score": row.get("shadow_score"),
        "selection_source": row.get("selection_source"),
        "track": row.get("track"),
        "stage": row.get("stage"),
        "tag": row.get("tag"),
        "candidate_lane": row.get("candidate_lane"),
        "entry_type": row.get("entry_type"),
        **_candidate_style_fields(row),
        **_candidate_theme_result_fields(row),
        "triggers": row.get("triggers"),
        **_candidate_quality_metrics(row),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


_CANDIDATE_QUALITY_METRIC_FIELDS = (
    "funnel_score",
    "candidate_shadow_score",
    "candidate_shadow_grade",
    "entry_quality_score",
    "entry_quality_grade",
    "entry_quality_risk_flags",
    "selection_strategy",
    "recommend_date",
    "is_ai_recommended",
    "recommend_count",
    "label_ready",
    "label_status",
)


def _candidate_quality_metrics(row: dict) -> dict[str, Any]:
    payload = {
        field: row.get(field) for field in _CANDIDATE_QUALITY_METRIC_FIELDS if row.get(field) not in (None, "", [])
    }
    payload.update(risk_adjusted_quality_metrics(row))
    return payload


def _candidate_quality_label(row: dict) -> str:
    priority = candidate_score_value(row.get("priority_score"))
    score = candidate_score_value(row.get("score"))
    trigger_count = len(row.get("triggers") or [])
    if _has_strong_quality_grade(row):
        return "高质量研报候选" if row.get("selected_for_report") else "高质量观察候选"
    if row.get("selected_for_report") and priority >= 10:
        return "高优先级研报候选"
    if row.get("selected_for_report"):
        return "研报候选"
    if score >= 8 or trigger_count >= 2:
        return "强观察候选"
    return "观察候选"


def _candidate_next_step(trade_mode: dict, bucket: str, data_quality: dict | None = None) -> str:
    if bucket == "watch":
        return "观察池跟踪，暂不进入本轮AI复核"
    mode = str(trade_mode.get("mode") or "").strip()
    if not bool(trade_mode.get("allow_ai_review")):
        return "只观察，等待市场风险闸门重新打开"
    if _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return "数据质量不足，先重跑或缩小扫描范围"
    if not bool(trade_mode.get("allow_recommendation_write")):
        return "可送AI做修复复核，但不写正式推荐"
    if mode == "confirmation_only":
        return "进入AI复核，等待二次确认后再行动"
    if mode == "repair_probe":
        return "进入AI复核，只允许一只候选以小额 PROBE 试探"
    if mode == "risk_on":
        return "进入AI复核，合格后纳入新买候选"
    return "进入AI复核，先确认候选质量"


def _candidate_action_status(trade_mode: dict, bucket: str, data_quality: dict | None = None) -> str:
    if bucket == "watch":
        return "watch_only"
    mode = str(trade_mode.get("mode") or "").strip()
    if not bool(trade_mode.get("allow_ai_review")):
        return "blocked_by_market_gate"
    if _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return "blocked_by_data_quality"
    if not bool(trade_mode.get("allow_recommendation_write")):
        return "repair_review_only"
    if mode == "confirmation_only":
        return "confirmation_required"
    if mode == "repair_probe":
        return "repair_probe_ready"
    return "ready_for_ai_review"


def _candidate_action_fields(trade_mode: dict, bucket: str, data_quality: dict | None = None) -> dict[str, Any]:
    return candidate_action_fields(
        {
            "action_status": _candidate_action_status(trade_mode, bucket, data_quality),
            "new_buy_allowed": bool(trade_mode.get("allow_recommendation_write")) and bucket != "watch",
            "direct_buy_allowed": False,
            "trade_readiness": _candidate_trade_readiness(trade_mode, bucket, data_quality),
        }
    )


def _candidate_trade_readiness(trade_mode: dict, bucket: str, data_quality: dict | None = None) -> str:
    if bucket == "watch":
        return "watch_only"
    if _data_quality_blocks_ready_flow(trade_mode, data_quality):
        return "data_quality_blocked"
    if not bool(trade_mode.get("allow_ai_review")):
        return "observe_only"
    if not bool(trade_mode.get("allow_recommendation_write")):
        return "research_only"
    if str(trade_mode.get("mode") or "").strip() == "confirmation_only":
        return "confirmation_required"
    if str(trade_mode.get("mode") or "").strip() == "repair_probe":
        return "probe_ready"
    return "review_ready"


def _candidate_profile(row: dict) -> str:
    parts = [
        _track_label(row.get("track")),
        _stage_label(row.get("stage")),
        _lane_profile(row),
        _candidate_theme_profile(row),
        source_label(str(row.get("selection_source") or "")),
        _trigger_profile(row.get("triggers")),
    ]
    return " / ".join(dict.fromkeys(part for part in parts if part))


def _candidate_quality_factors(row: dict, *, next_step: str = "") -> list[str]:
    factors = [_candidate_quality_label(row)]
    factors.extend(_candidate_grade_quality_factors(row))
    factors.extend(_split_factor_text(_candidate_profile(row), " / "))
    factors.extend(str(item) for item in row.get("style_match_reasons") or [] if str(item))
    factors.extend(str(item) for item in row.get("theme_match_reasons") or [] if str(item))
    factors.extend(_split_factor_text(str(row.get("rank_reason") or ""), "；"))
    if next_step:
        factors.append(next_step)
    return list(dict.fromkeys(factor for factor in factors if factor))


def _has_strong_quality_grade(row: dict) -> bool:
    shadow_grade = str(row.get("candidate_shadow_grade") or "").strip().upper()
    entry_grade = str(row.get("entry_quality_grade") or "").strip().upper()
    return shadow_grade == "S" or entry_grade in {"S", "A"}


def _candidate_grade_quality_factors(row: dict) -> list[str]:
    factors: list[str] = []
    shadow_grade = str(row.get("candidate_shadow_grade") or "").strip().upper()
    entry_grade = str(row.get("entry_quality_grade") or "").strip().upper()
    if shadow_grade:
        factors.append(f"候选影子评级 {shadow_grade}")
    if entry_grade and entry_grade != "UNKNOWN":
        factors.append(f"入场质量评级 {entry_grade}")
    return factors


def _candidate_risk_factors(
    row: dict,
    trade_mode: dict | None = None,
    bucket: str = "",
    data_quality: dict | None = None,
) -> list[str]:
    factors: list[str] = []
    factors.extend(str(item) for item in row.get("risk_factors") or [])
    if bucket == "watch" or not bool(row.get("selected_for_report")):
        factors.append("未进入本轮研报候选")
    if not row.get("triggers") and not row.get("selected_for_report"):
        factors.append("触发信号未列明")
    factors.extend(entry_quality_risk_flags(row.get("entry_quality_risk_flags")))
    if reason := ai_review_quality_gate_reason(row, candidate_ai_review_label(row)):
        factors.append(reason)
    if trade_mode:
        if _data_quality_blocks_ready_flow(trade_mode, data_quality) and bucket != "watch":
            factors.extend(_data_quality_risk_factors(data_quality))
        factors.extend(_trade_mode_risk_factors(trade_mode, bucket))
    return list(dict.fromkeys(factor for factor in factors if factor))


def _candidate_theme_profile(row: dict) -> str:
    theme = str(row.get("strategic_theme") or row.get("theme") or "").strip()
    if not theme:
        return ""
    source = str(row.get("theme_source") or "").strip()
    return f"事件主线:{theme}" if source == "ths_hot_event" else f"主题:{theme}"


def _trade_mode_risk_factors(trade_mode: dict, bucket: str) -> list[str]:
    if bucket == "watch":
        return ["观察池，不进入本轮AI复核"]
    if not bool(trade_mode.get("allow_ai_review")):
        return [str(trade_mode.get("reason") or "市场风险闸门未打开")]
    if not bool(trade_mode.get("allow_recommendation_write")):
        return ["只做修复复核，不写正式推荐"]
    if str(trade_mode.get("mode") or "").strip() == "confirmation_only":
        return ["等待二次确认后再行动"]
    if str(trade_mode.get("mode") or "").strip() == "repair_probe":
        return ["修复刚成立，仅允许小额 PROBE，禁止 ATTACK"]
    return []


def _split_factor_text(text: str, sep: str) -> list[str]:
    return [part.strip() for part in str(text or "").split(sep) if part.strip()]


def _lane_profile(row: dict) -> str:
    return lane_label(str(row.get("candidate_lane") or row.get("entry_type") or "").strip())


def _track_label(raw: object) -> str:
    return {"Trend": "趋势线", "Accum": "吸筹线"}.get(str(raw or "").strip(), "")


def _stage_label(raw: object) -> str:
    stage = str(raw or "").strip()
    return {"Markup": "主升阶段", "Accum_B": "吸筹B段", "Accum_C": "吸筹C段"}.get(stage, stage)


def _trigger_profile(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    labels = [TRIGGER_SHORT_LABELS.get(str(trigger), str(trigger)) for trigger in raw[:4] if str(trigger)]
    return f"触发:{'+'.join(labels)}" if labels else ""


def _ranked_candidates(
    trigger_groups: dict,
    symbols_for_report: list[Any],
    name_map: dict,
    details: dict | None = None,
    *,
    limit: int = 20,
) -> list[dict]:
    selected_rows = _report_rows(symbols_for_report)
    selected = set(selected_rows)
    priority_scores = _priority_score_map(details or {}, selected_rows)
    shadow_scores = _shadow_score_map(details or {})
    metadata_map = _candidate_metadata_map(details or {})
    rows: dict[str, dict] = {}
    for trigger_name, candidates in trigger_groups.items():
        for candidate in candidates:
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            row = rows.setdefault(
                code,
                _candidate_row(
                    code,
                    candidate.get("name") or name_map.get(code),
                    selected_rows.get(code),
                    selected,
                    shadow_scores.get(code),
                    _metadata_for_code(metadata_map, code),
                ),
            )
            row["score"] = max(float(row["score"]), candidate_score_value(candidate.get("score")))
            row["priority_score"] = max(float(row["priority_score"]), candidate_score_value(priority_scores.get(code)))
            if trigger_name not in row["triggers"]:
                row["triggers"].append(trigger_name)
    for code, report_row in selected_rows.items():
        row = rows.setdefault(
            code,
            _candidate_row(
                code,
                name_map.get(code),
                report_row,
                selected,
                shadow_scores.get(code),
                _metadata_for_code(metadata_map, code),
            ),
        )
        row["priority_score"] = max(float(row["priority_score"]), candidate_score_value(priority_scores.get(code)))
    ranked = list(rows.values())
    ranked.sort(key=_candidate_sort_key)
    return [_final_candidate_row(row) for row in ranked[:limit]]


def _candidate_metadata_map(details: dict) -> dict[str, dict[str, Any]]:
    return build_candidate_metadata_map(
        _safe_dict_list(details.get("candidate_entries")),
        _safe_dict_list(details.get("mainline_candidates")),
    )


def _safe_dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _metadata_for_code(metadata_map: dict[str, dict[str, Any]], code: str) -> dict[str, Any]:
    return metadata_map.get(code6(code)) or metadata_map.get(str(code or "").strip()) or {}


def _report_rows(symbols_for_report: list[Any]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for order, item in enumerate(symbols_for_report or [], start=1):
        code = _symbol_code(item)
        if not code:
            continue
        row = dict(item) if isinstance(item, dict) else {}
        row["_report_order"] = order
        rows.setdefault(code, row)
    return rows


def _symbol_code(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("code") or item.get("symbol") or "").strip()
    return str(item or "").strip()


def _priority_score_map(details: dict, selected_rows: dict[str, dict]) -> dict[str, float]:
    raw = details.get("priority_score_map") if isinstance(details, dict) else {}
    scores = {str(code): candidate_score_value(score) for code, score in (raw or {}).items()}
    for code, row in selected_rows.items():
        scores[code] = max(candidate_score_value(scores.get(code)), candidate_score_value(row.get("priority_score")))
    return scores


def _shadow_score_map(details: dict) -> dict[str, float]:
    raw = details.get("shadow_score_map") if isinstance(details, dict) else {}
    return {str(code): candidate_score_value(score) for code, score in (raw or {}).items()}


def _candidate_row(
    code: str,
    name: object,
    report_row: dict | None,
    selected: set[str],
    shadow_score: object = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    report_row = report_row or {}
    metadata = metadata or {}
    return {
        "code": code,
        "name": str(report_row.get("name") or name or code),
        "score": 0.0,
        "priority_score": candidate_score_value(report_row.get("priority_score")),
        "shadow_score": candidate_score_value(shadow_score),
        "priority_rank": int(report_row.get("priority_rank") or report_row.get("_report_order") or 0),
        "triggers": [],
        "selected_for_report": code in selected,
        "selection_source": str(report_row.get("selection_source") or _metadata_selection_source(metadata)).strip(),
        "track": str(report_row.get("track") or _metadata_track(metadata)).strip(),
        "stage": str(report_row.get("stage") or metadata.get("candidate_status") or "").strip(),
        "tag": str(report_row.get("tag") or _metadata_tag(metadata)).strip(),
        "candidate_lane": str(report_row.get("candidate_lane") or metadata.get("candidate_lane") or "").strip(),
        "entry_type": str(report_row.get("entry_type") or metadata.get("entry_type") or "").strip(),
        **_candidate_theme_fields(report_row, metadata),
        **_candidate_quality_metrics(report_row),
    }


def _candidate_theme_fields(report_row: dict, metadata: dict[str, Any]) -> dict[str, Any]:
    reasons = metadata.get("candidate_reasons") if isinstance(metadata.get("candidate_reasons"), dict) else {}
    metrics = metadata.get("candidate_metrics") if isinstance(metadata.get("candidate_metrics"), dict) else {}
    return _drop_empty_candidate_fields(
        {
            "strategic_theme": report_row.get("strategic_theme") or reasons.get("theme"),
            "theme_score": report_row.get("theme_score") or metadata.get("theme_score"),
            "theme_source": report_row.get("theme_source")
            or reasons.get("theme_source")
            or metrics.get("theme_source"),
            "theme_event_id": report_row.get("theme_event_id") or reasons.get("theme_event_id"),
            "theme_event_date": report_row.get("theme_event_date") or reasons.get("theme_event_date"),
            "theme_event_title": report_row.get("theme_event_title") or reasons.get("theme_event_title"),
            "theme_event_reason": report_row.get("theme_event_reason") or reasons.get("theme_event_reason"),
        }
    )


def _candidate_theme_result_fields(row: dict) -> dict[str, Any]:
    fields = (
        "strategic_theme",
        "theme_score",
        "theme_source",
        "theme_event_id",
        "theme_event_date",
        "theme_event_title",
        "theme_event_reason",
    )
    return _drop_empty_candidate_fields({field: row.get(field) for field in fields})


def _candidate_style_fields(row: dict) -> dict[str, Any]:
    fields = (
        "style_match",
        "style_match_styles",
        "style_match_score",
        "style_match_reasons",
        "theme_match",
        "theme_match_score",
        "theme_match_reasons",
    )
    return _drop_empty_candidate_fields({field: row.get(field) for field in fields})


def _metadata_selection_source(metadata: dict[str, Any]) -> str:
    lane = str(metadata.get("candidate_lane") or "").strip()
    if lane == "mainline":
        return "mainline"
    return "alpha_candidate" if metadata else ""


def _metadata_track(metadata: dict[str, Any]) -> str:
    if not any(metadata.get(field) for field in ("candidate_lane", "signal_key", "entry_type")):
        return ""
    return candidate_entry_track(metadata, default="", fields=("candidate_lane", "signal_key", "entry_type"))


def _metadata_tag(metadata: dict[str, Any]) -> str:
    return str(metadata.get("entry_type") or metadata.get("signal_key") or metadata.get("candidate_lane") or "").strip()


def _candidate_sort_key(row: dict) -> tuple:
    priority_rank = int(row.get("priority_rank") or 999999)
    return (
        not bool(row["selected_for_report"]),
        priority_rank,
        -candidate_score_value(row.get("priority_score")),
        -risk_adjusted_quality_score(row),
        -candidate_score_value(row.get("score")),
        -candidate_score_value(row.get("shadow_score")),
        row["code"],
    )


def _apply_style_preference(candidates: list[dict], preference: dict[str, Any]) -> list[dict]:
    styles = [str(item) for item in preference.get("styles") or [] if str(item)]
    if not styles:
        return candidates
    ranked = [(index, _annotate_candidate_style_match(row, styles)) for index, row in enumerate(candidates)]
    ranked.sort(
        key=lambda item: (
            not bool(item[1].get("selected_for_report")),
            -_style_match_coverage_score(item[1], styles),
            -_style_match_score(item[1]),
            item[0],
        )
    )
    return [row for _index, row in ranked]


def _apply_theme_preference(candidates: list[dict], preference: dict[str, Any]) -> list[dict]:
    theme = str(preference.get("theme") or "").strip()
    if not theme:
        return candidates
    ranked = [(index, _annotate_candidate_theme_match(row, preference)) for index, row in enumerate(candidates)]
    ranked.sort(
        key=lambda item: (
            not bool(item[1].get("theme_match")),
            not bool(item[1].get("selected_for_report")),
            -int(item[1].get("theme_match_score") or 0),
            item[0],
        )
    )
    return [row for _index, row in ranked]


def _preference_match_summary(
    style_preference: dict[str, Any], theme_preference: dict[str, Any], candidates: list[dict]
) -> dict[str, str]:
    return _drop_empty_candidate_fields(
        {
            "style": _style_preference_match_status(candidates, style_preference)
            if _has_style_preference(style_preference)
            else "",
            "theme": _preference_match_status(candidates, "theme") if _has_theme_preference(theme_preference) else "",
        }
    )


def _annotate_preference_miss_risks(
    candidates: list[dict],
    style_preference: dict[str, Any],
    theme_preference: dict[str, Any],
) -> list[dict]:
    if not _has_style_preference(style_preference) and not _has_theme_preference(theme_preference):
        return candidates
    out: list[dict] = []
    for row in candidates:
        risks = _candidate_preference_miss_risk_factors(row, style_preference, theme_preference)
        if not risks:
            out.append(row)
            continue
        payload = dict(row)
        payload["risk_factors"] = list(dict.fromkeys([*payload.get("risk_factors", []), *risks]))
        out.append(payload)
    return out


def _candidate_preference_miss_risk_factors(
    row: dict,
    style_preference: dict[str, Any],
    theme_preference: dict[str, Any],
) -> list[str]:
    risks: list[str] = []
    if labels := _candidate_missing_style_preference_labels(row, style_preference):
        risks.append(f"风格偏好未命中: {'/'.join(labels)}" if labels else "风格偏好未命中")
    if _has_theme_preference(theme_preference) and not _candidate_matches_preference(row, "theme"):
        theme = str(theme_preference.get("theme") or theme_preference.get("raw") or "").strip()
        risks.append(f"主题偏好未命中: {theme}" if theme else "主题偏好未命中")
    return risks


def _annotate_candidate_theme_match(row: dict, preference: dict[str, Any]) -> dict:
    reasons = _theme_match_reasons(row, preference)
    if not reasons:
        return row
    payload = dict(row)
    payload["theme_match"] = True
    payload["theme_match_score"] = len(reasons)
    payload["theme_match_reasons"] = reasons[:4]
    payload["quality_factors"] = list(dict.fromkeys([*payload.get("quality_factors", []), *reasons[:3]]))
    return payload


def _theme_match_reasons(row: dict, preference: dict[str, Any]) -> list[str]:
    theme = str(preference.get("theme") or "").strip()
    if not theme:
        return []
    terms = _theme_preference_terms(preference)
    text = _candidate_theme_match_text(row)
    reasons = []
    row_theme = normalize_theme_name(str(row.get("strategic_theme") or row.get("theme") or ""))
    if row_theme and row_theme == theme:
        reasons.append(f"主题偏好: {theme}")
    if any(term and term.lower() in text for term in terms):
        reasons.append(f"主题偏好: {theme}")
    return list(dict.fromkeys(reasons))


def _theme_preference_terms(preference: dict[str, Any]) -> list[str]:
    theme = str(preference.get("theme") or "").strip()
    raw = str(preference.get("raw") or "").strip()
    aliases = THEME_ALIASES.get(theme, ())
    return [item for item in dict.fromkeys((theme, raw, *aliases)) if len(str(item).strip()) >= 2]


def _candidate_theme_match_text(row: dict) -> str:
    values: list[str] = []
    for field in (
        "strategic_theme",
        "theme",
        "theme_event_title",
        "theme_event_reason",
        "candidate_lane",
        "entry_type",
        "tag",
        "name",
    ):
        values.append(str(row.get(field) or ""))
    values.extend(str(item) for item in row.get("quality_factors") or [])
    values.extend(str(item) for item in row.get("triggers") or [])
    return " ".join(values).lower()


def _annotate_candidate_style_match(row: dict, styles: list[str]) -> dict:
    matched_styles, reasons = _style_match_details(row, styles)
    if not reasons:
        return row
    payload = dict(row)
    payload["style_match"] = True
    payload["style_match_styles"] = matched_styles
    payload["style_match_score"] = len(reasons)
    payload["style_match_reasons"] = reasons[:4]
    payload["quality_factors"] = list(dict.fromkeys([*payload.get("quality_factors", []), *reasons[:3]]))
    return payload


def _style_match_reasons(row: dict, styles: list[str]) -> list[str]:
    return _style_match_details(row, styles)[1]


def _style_match_details(row: dict, styles: list[str]) -> tuple[list[str], list[str]]:
    matched_styles: list[str] = []
    reasons: list[str] = []
    for style in styles:
        style_reasons = _style_reasons(row, style)
        if not any(style_reasons):
            continue
        matched_styles.append(style)
        reasons.extend(style_reasons)
    return list(dict.fromkeys(matched_styles)), list(dict.fromkeys(reason for reason in reasons if reason))


def _style_reasons(row: dict, style: str) -> list[str]:
    if style == "trend":
        return _trend_style_reasons(row)
    if style == "pullback":
        return _pullback_style_reasons(row)
    if style == "quality":
        return _quality_style_reasons(row)
    return []


def _trend_style_reasons(row: dict) -> list[str]:
    triggers = {str(item).lower() for item in row.get("triggers") or []}
    return [
        "趋势偏好: 趋势线" if row.get("track") == "Trend" else "",
        "趋势偏好: 主升阶段" if row.get("stage") == "Markup" else "",
        "趋势偏好: SOS触发" if "sos" in triggers else "",
    ]


def _pullback_style_reasons(row: dict) -> list[str]:
    text = " ".join(str(row.get(field) or "").lower() for field in ("candidate_lane", "entry_type", "tag"))
    return [
        "低吸偏好: 吸筹线" if row.get("track") == "Accum" else "",
        "低吸偏好: 吸筹阶段" if str(row.get("stage") or "").startswith("Accum") else "",
        "低吸偏好: 回踩/跳板" if any(token in text for token in ("lps", "springboard", "低吸", "回踩")) else "",
    ]


def _quality_style_reasons(row: dict) -> list[str]:
    shadow_grade = str(row.get("candidate_shadow_grade") or "").upper()
    entry_grade = str(row.get("entry_quality_grade") or "").upper()
    return [
        "稳健偏好: 候选影子S级" if shadow_grade == "S" else "",
        "稳健偏好: 入场质量A档以上" if entry_grade in {"S", "A"} else "",
        "稳健偏好: 风险调整质量较高" if risk_adjusted_quality_score(row) >= 80 else "",
    ]


def _style_match_score(row: dict) -> int:
    return int(row.get("style_match_score") or 0)


def _style_match_coverage_score(row: dict, styles: list[str]) -> int:
    return len(_candidate_style_match_styles(row, styles))


def _final_candidate_row(row: dict) -> dict:
    rank_reason = _rank_reason(row)
    payload = {
        "code": row["code"],
        "name": row["name"],
        "score": round(candidate_score_value(row.get("score")), 2),
        "priority_score": round(candidate_score_value(row.get("priority_score")), 2),
        "priority_rank": int(row["priority_rank"]) if row.get("priority_rank") else None,
        "triggers": list(row["triggers"]),
        "selected_for_report": bool(row["selected_for_report"]),
        "selection_source": row["selection_source"],
        "track": row["track"],
        "stage": row["stage"],
        "tag": row["tag"],
        "candidate_lane": row["candidate_lane"],
        "entry_type": row["entry_type"],
        "rank_reason": rank_reason,
        **_candidate_theme_result_fields(row),
    }
    if candidate_score_value(row.get("shadow_score")):
        payload["shadow_score"] = round(candidate_score_value(row.get("shadow_score")), 2)
    payload.update(_candidate_quality_metrics(row))
    payload["quality_factors"] = _candidate_quality_factors(payload)
    payload["risk_factors"] = _candidate_risk_factors(payload)
    return payload


def _rank_reason(row: dict) -> str:
    parts: list[str] = []
    if row["selected_for_report"]:
        rank = int(row.get("priority_rank") or 0)
        parts.append(f"研报候选#{rank}" if rank else "研报候选")
    if candidate_score_value(row.get("priority_score")):
        parts.append(f"优先分 {candidate_score_value(row.get('priority_score')):.2f}")
    if risk_adjusted_quality_score(row):
        parts.append(f"质量分 {risk_adjusted_quality_score(row):.2f}")
    if entry_quality_risk_penalty(row):
        parts.append(f"入场风险扣减 {entry_quality_risk_penalty(row):.2f}")
    if candidate_score_value(row.get("shadow_score")):
        parts.append(f"动态策略分 {candidate_score_value(row.get('shadow_score')):.2f}")
    if row["triggers"]:
        labels = [TRIGGER_SHORT_LABELS.get(str(trigger), str(trigger)) for trigger in row["triggers"]]
        parts.append("+".join(labels))
    return "；".join(parts) or "触发候选"
