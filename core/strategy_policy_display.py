"""Display helpers for strategy policy weights."""

from __future__ import annotations

import math
from typing import Any

# 回测里归因调权按设计不接报告时的措辞。
#
# 只有一处定义:这条串要被 core/backtest_report 渲染进 markdown、被
# workflows/backtest_market_report_artifacts 解析回 GridCell、再被
# workflows/backtest_market_report_builder 的确认块前缀匹配。三处各抄一份字面量,
# 改一处就会让确认块把「按设计关闭」重新读成「该开却没开」并挂上没人能消的待查。
POLICY_OFF_BY_DESIGN = "不接归因报告（回测避免前视）"


def policy_weight_rows(weights: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for key, raw_weight in sorted((weights or {}).items()):
        parsed = parse_policy_weight_key(key)
        weight = safe_policy_weight(raw_weight)
        rows.append(
            {
                "key": str(key),
                "signal_type": parsed["signal_type"],
                "scope": parsed["scope"],
                "label": format_policy_signal_label(parsed["signal_type"], parsed["scope"]),
                "weight": weight,
                "direction": "down" if weight < 1.0 else "up" if weight > 1.0 else "flat",
            }
        )
    return rows


def format_policy_weight_text(weights: dict[str, Any] | None, *, limit: int = 8, delimiter: str = "，") -> str:
    rows = policy_weight_rows(weights)
    parts = [_format_weight_row(row) for row in rows[: max(int(limit), 0)]]
    if len(rows) > limit:
        parts.append(f"等{len(rows)}项")
    return delimiter.join(parts)


def format_policy_meta_text(meta: dict[str, Any] | None) -> str:
    if not isinstance(meta, dict) or not meta:
        return ""
    tokens = _policy_source_tokens(meta)
    active = _policy_active_scope(meta)
    if active:
        tokens.append(f"范围={active}")
    formal_block = str(meta.get("formal_dynamic_block_reason") or "").strip()
    if meta.get("formal_dynamic_allowed") is False and formal_block:
        tokens.append(f"正式dynamic={policy_formal_dynamic_label(meta)}")
    backtest = str(meta.get("backtest_confirmation_text") or "").strip()
    if backtest:
        tokens.append(f"回测={backtest}")
    checklist = str(meta.get("promotion_checklist_summary") or "").strip()
    if checklist:
        tokens.append(f"晋级={checklist}")
    return f"（{', '.join(tokens)}）" if tokens else ""


def parse_policy_weight_key(raw: Any) -> dict[str, Any]:
    parts = [part.strip() for part in str(raw or "").split("|") if part.strip()]
    signal = parts[0] if parts else ""
    scope: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            continue
        scope[_scope_key(key)] = value
    return {"signal_type": signal, "scope": scope}


def format_policy_signal_label(signal_type: Any, scope: dict[str, Any] | None = None) -> str:
    signal = str(signal_type or "").strip() or "unknown"
    scope_text = format_policy_scope(scope)
    return f"{signal}[{scope_text}]" if scope_text else signal


def format_policy_scope(scope: dict[str, Any] | None) -> str:
    row = scope or {}
    parts = []
    regime = str(row.get("regime") or "").strip()
    lane = str(row.get("lane") or "").strip()
    entry = str(row.get("entry_type") or row.get("entry") or "").strip()
    if regime:
        parts.append(f"regime={regime}")
    if lane:
        parts.append(f"lane={lane}")
    if entry:
        parts.append(f"entry={entry}")
    return ", ".join(parts)


def safe_policy_weight(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return value if math.isfinite(value) else 1.0


def _format_weight_row(row: dict[str, Any]) -> str:
    marker = "↓" if row.get("direction") == "down" else "↑" if row.get("direction") == "up" else ""
    return f"{row.get('label')}×{safe_policy_weight(row.get('weight')):.2f}{marker}"


def _policy_source_tokens(meta: dict[str, Any]) -> list[str]:
    tokens = []
    source = str(meta.get("source") or "").strip()
    report_date = str(meta.get("report_date") or "").strip()
    horizon = str(meta.get("horizon") or "").strip()
    if source:
        tokens.append(source)
    if report_date:
        tokens.append(f"报告={report_date}")
    if horizon:
        tokens.append(f"周期=h{horizon}")
    age = meta.get("age_days")
    if age is not None and str(age) != "":
        tokens.append(f"距今={age}天")
    execution_policy = str(meta.get("execution_policy") or "").strip()
    if execution_policy:
        tokens.append(f"策略={policy_execution_mode_label(execution_policy)}")
    next_action = str(meta.get("next_action") or "").strip()
    if next_action:
        tokens.append(f"下一步={policy_next_action_label(next_action)}")
    return tokens


def _policy_active_scope(meta: dict[str, Any]) -> str:
    explicit = str(meta.get("active_scope") or meta.get("policy_weight_active_scope") or "").strip()
    if explicit and explicit != "无":
        return explicit
    if meta.get("funnel_formal_weights_active") is True:
        return "正式漏斗"
    if meta.get("funnel_shadow_weights_active") is True:
        return "漏斗shadow"
    scope = str(meta.get("execution_scope") or meta.get("policy_weight_execution_scope") or "").strip()
    if scope == "funnel_formal":
        return "正式漏斗"
    if scope == "funnel_shadow":
        return "漏斗shadow"
    return ""


def policy_next_action_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "manual_review_dynamic_on": "进入人工晋级评审（非正式生效）",
        "formal_dynamic_approved": "正式 dynamic 已人工批准",
        "run_backtest_confirmation": "先跑回测确认",
        "keep_shadow_backtest_failed": "回测未通过，保持 shadow",
        "review_policy_actions": "先复核调权治理项",
        "keep_static_policy": "保持静态策略",
        "collect_more_shadow_samples": "继续收集样本",
        "keep_shadow_apply_signal_weights": "保持 shadow 并应用信号级调权",
        "keep_shadow_observe": "保持 shadow 观察",
    }
    return labels.get(text, text or "保持观察")


def policy_mode_recommendation_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "review_promote_dynamic_policy": "评审是否切 on",
        "keep_shadow": "保持 shadow",
        "keep_static_policy": "保持静态策略",
    }
    return labels.get(text, text or "保持 shadow")


def policy_execution_mode_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "on": "正式调权(on)",
        "shadow": "shadow 对照(shadow)",
        "off": "静态策略(off)",
        "unknown": "未知模式",
    }
    return labels.get(text, f"{text} 模式" if text else "未知模式")


def policy_promotion_status_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "manual_review_required": "需人工复核",
        "manual_approved": "已人工批准",
        "do_not_promote": "禁止晋级",
        "collect_more_samples": "继续收集样本",
        "keep_shadow": "保持 shadow",
    }
    return labels.get(text, text or "未知")


def policy_governor_status_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "candidate": "可进入人工晋级评审",
        "watch": "继续观察",
        "reject": "不建议晋级",
        "insufficient_sample": "样本不足",
    }
    return labels.get(text, text or "未知")


def policy_formal_dynamic_label(execution: dict[str, Any] | None) -> str:
    row = execution or {}
    if row.get("formal_dynamic_allowed") is True:
        return "允许正式生效"
    if row.get("formal_dynamic_allowed") is False:
        reason = str(row.get("formal_dynamic_block_reason") or "").strip()
        return f"未进正式漏斗({_formal_dynamic_reason_label(reason)})" if reason else "未进正式漏斗"
    if str(row.get("next_action") or "").strip() == "manual_review_dynamic_on":
        return "未进正式漏斗(人工复核未完成)"
    if str(row.get("next_action") or "").strip() == "review_policy_actions":
        return "未进正式漏斗(调权治理项待复核)"
    if str(row.get("next_action") or "").strip() == "formal_dynamic_approved":
        return "允许正式生效"
    return "未知"


def _formal_dynamic_reason_label(reason: str) -> str:
    labels = {
        "auto_apply=false": "未启用自动晋级",
        "backtest_confirmation_failed": "回测未通过",
        "backtest_confirmation_required": "缺少回测确认",
        "backtest_policy_evidence_required": "回测缺少策略治理证据",
        "execution_state=missing": "缺少后端执行态",
        "formal_dynamic_allowed=false": "治理器未放行",
        "manual_approval_incomplete": "人工批准证据不完整",
        "manual_review_required": "人工复核未完成",
        "promotion_checklist=missing": "晋级清单缺失",
        "shadow_only": "仅 shadow 观察",
        "selection_actions_review_required": "候选源治理待复核",
        "signal_actions_review_required": "信号调权待复核",
    }
    if reason in labels:
        return labels[reason]
    if reason.startswith("next_action="):
        return f"下一步={policy_next_action_label(reason.split('=', 1)[1])}"
    if reason.startswith("promotion_status="):
        return f"晋级状态={policy_promotion_status_label(reason.split('=', 1)[1])}"
    if reason.startswith("promotion_checklist="):
        details = reason.split("=", 1)[1]
        return f"晋级清单未通过({_promotion_checklist_detail_label(details)})" if details else "晋级清单未通过"
    return reason


def _promotion_checklist_detail_label(details: str) -> str:
    parts = []
    for item in [x.strip() for x in details.split(",") if x.strip()]:
        key, _, status = item.partition(":")
        if status:
            parts.append(f"{policy_promotion_check_key_label(key)}:{policy_promotion_check_status_label(status)}")
        else:
            parts.append(policy_promotion_check_key_label(key))
    return "，".join(parts) if parts else details


def policy_promotion_check_key_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "shadow_sample": "样本",
        "shadow_performance": "Shadow表现",
        "shadow_added_outperforms_removed": "新增跑赢",
        "selection_actions": "候选源治理",
        "signal_actions": "信号调权",
        "backtest_confirmation": "回测",
    }
    return labels.get(text, text or "-")


def policy_promotion_check_status_label(raw: Any) -> str:
    text = str(raw or "").strip()
    labels = {
        "pass": "通过",
        "fail": "失败",
        "review": "待复核",
        "missing": "缺失",
        "not_required": "不需要",
        "unknown": "未知",
    }
    return labels.get(text, text or "未知")


def policy_governor_display(governor: dict[str, Any] | None) -> dict[str, str]:
    row = governor or {}
    return {
        "status": policy_governor_status_label(row.get("status")),
        "mode_recommendation": policy_mode_recommendation_label(row.get("mode_recommendation")),
        "next_action": policy_next_action_label(row.get("next_action")),
        "promotion_status": policy_promotion_status_label(row.get("promotion_status")),
        "auto_apply": "是" if row.get("auto_apply") else "否",
    }


def policy_execution_display(execution: dict[str, Any] | None) -> dict[str, str]:
    row = execution or {}
    return {
        "active_scope": str(row.get("active_scope") or "无"),
        "promotion_status": policy_promotion_status_label(row.get("promotion_status")),
        "next_action": policy_next_action_label(row.get("next_action")),
        "formal_dynamic": policy_formal_dynamic_label(row),
        "summary": str(row.get("summary") or ""),
    }


def _scope_key(raw: str) -> str:
    key = raw.strip().lower()
    return "entry_type" if key == "entry" else key
