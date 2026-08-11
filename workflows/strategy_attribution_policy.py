"""Read-only strategy attribution policy inputs."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from core.constants import TABLE_STRATEGY_ATTRIBUTION_REPORTS
from core.strategy_policy_display import (
    format_policy_meta_text,
    format_policy_weight_text,
)
from core.strategy_policy_governor import signal_weight_multipliers_from_rows
from integrations.supabase_base import close_client, create_read_client
from workflows.strategy_attribution_execution import (
    attribution_execution_state,
    attribution_formal_dynamic_allowed,
    attribution_operations_brief,
)

LOCAL_ATTRIBUTION_REPORT = Path("/private/tmp/wyckoff-strategy-attribution/latest/report.json")


@dataclass(frozen=True)
class AttributionPolicySnapshot:
    weights: dict[str, float] = field(default_factory=dict)
    source: str = ""
    report_date: str = ""
    horizon: str = "5"
    age_days: int | None = None
    max_age_days: int = 7
    governor_status: str = ""
    mode_recommendation: str = ""
    next_action: str = ""
    next_action_summary: str = ""
    auto_apply: bool = False
    execution_policy: str = ""
    execution_scope: str = ""
    formal_dynamic_allowed: bool = False
    formal_dynamic_block_reason: str = ""
    promotion_checklist_summary: str = ""
    backtest_confirmation_text: str = ""
    signal_action_count: int = 0
    selection_action_count: int = 0
    selection_action_summary: str = ""
    execution_summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        has_weights = bool(self.weights)
        shadow_active = has_weights and self.execution_scope == "funnel_shadow"
        formal_active = has_weights and self.execution_scope == "funnel_formal"
        return {
            "source": self.source,
            "report_date": self.report_date,
            "horizon": self.horizon,
            "age_days": self.age_days,
            "max_age_days": self.max_age_days,
            "weight_count": len(self.weights),
            "governor_status": self.governor_status,
            "mode_recommendation": self.mode_recommendation,
            "next_action": self.next_action,
            "next_action_summary": self.next_action_summary,
            "auto_apply": self.auto_apply,
            "execution_policy": self.execution_policy,
            "execution_scope": self.execution_scope,
            "formal_dynamic_allowed": self.formal_dynamic_allowed,
            "formal_dynamic_block_reason": self.formal_dynamic_block_reason,
            "promotion_checklist_summary": self.promotion_checklist_summary,
            "backtest_confirmation_text": self.backtest_confirmation_text,
            "signal_action_count": self.signal_action_count,
            "selection_action_count": self.selection_action_count,
            "selection_action_summary": self.selection_action_summary,
            "execution_summary": self.execution_summary,
            "active_scope": _active_scope_text(shadow_active=shadow_active, formal_active=formal_active),
            "funnel_shadow_weights_active": shadow_active,
            "funnel_formal_weights_active": formal_active,
        }


def load_attribution_policy_snapshot(
    *,
    market: str = "cn",
    log_fn: Callable[[str], None] | None = None,
    as_of: date | None = None,
) -> AttributionPolicySnapshot:
    today = as_of or date.today()
    max_age = _max_report_age_days()
    try:
        row = load_latest_attribution_report(market)
    except Exception as exc:
        _log(log_fn, f"策略归因调权读取失败，跳过: {exc}")
        row = None
    row, source = _select_fresh_report(row, market=market, today=today, log_fn=log_fn)
    if not row:
        _log(log_fn, "策略归因调权: 暂无归因报告，跳过。")
        return AttributionPolicySnapshot(max_age_days=max_age)
    horizon = policy_horizon(row)
    weights = signal_weight_multipliers_from_rows(row.get("recommendations_json"), horizon=horizon)
    snapshot = _policy_snapshot(row, weights, source=source, today=today, max_age=max_age, horizon=horizon)
    if weights:
        _log(log_fn, "策略归因调权: " + _snapshot_log_text(snapshot))
    elif snapshot.selection_action_count:
        _log(log_fn, f"策略归因治理: {snapshot.selection_action_summary}")
    else:
        _log(log_fn, "策略归因调权: 最新报告无可执行信号权重。")
    return snapshot


def load_latest_attribution_report(market: str) -> dict[str, Any] | None:
    client = create_read_client()
    try:
        rows = (
            client.table(TABLE_STRATEGY_ATTRIBUTION_REPORTS)
            .select("report_date,created_at,market,shadow_diff_stats_json,recommendations_json")
            .eq("market", market)
            .order("report_date", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None
    finally:
        close_client(client)


def attribution_weights_for_funnel(
    snapshot: AttributionPolicySnapshot,
    *,
    mode: str,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, float]:
    weights = dict(snapshot.weights or {})
    normalized_mode = _normalize_funnel_mode(mode)
    if not weights or normalized_mode == "off":
        return {}
    if normalized_mode == "shadow":
        return weights
    if normalized_mode == "on" and snapshot.formal_dynamic_allowed:
        return weights
    reason = snapshot.formal_dynamic_block_reason or "governor_not_approved"
    _log(log_fn, f"策略归因调权: formal dynamic 未启用归因权重({reason})，仅保留漏斗shadow语义。")
    return {}


def load_local_attribution_report(market: str) -> dict[str, Any] | None:
    path = Path(
        os.getenv("STRATEGY_ATTRIBUTION_REPORT_JSON")
        or os.getenv("TAIL_BUY_ATTRIBUTION_REPORT_JSON")
        or LOCAL_ATTRIBUTION_REPORT
    )
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict) or str(row.get("market") or market) != market:
        return None
    return row


def policy_horizon(row: dict[str, Any]) -> str:
    shadow = row.get("shadow_diff_stats_json")
    if isinstance(shadow, dict):
        governor = shadow.get("policy_governor")
        if isinstance(governor, dict) and governor.get("horizon"):
            return str(governor["horizon"])
    return "5"


def _select_fresh_report(
    remote_row: dict[str, Any] | None,
    *,
    market: str,
    today: date,
    log_fn: Callable[[str], None] | None,
) -> tuple[dict[str, Any] | None, str]:
    remote = _fresh_report(remote_row, today=today, log_fn=log_fn, source="远端")
    local = _fresh_report(load_local_attribution_report(market), today=today, log_fn=log_fn, source="本地")
    if _has_explicit_local_report_path():
        return _newest_report_source(remote, local)
    if remote:
        return remote, "远端"
    return (local, "本地") if local else (None, "")


def _has_explicit_local_report_path() -> bool:
    return bool(os.getenv("STRATEGY_ATTRIBUTION_REPORT_JSON") or os.getenv("TAIL_BUY_ATTRIBUTION_REPORT_JSON"))


def _newest_report_source(
    remote: dict[str, Any] | None,
    local: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    candidates = [(remote, "远端"), (local, "本地")]
    candidates = [(row, source) for row, source in candidates if row]
    if not candidates:
        return None, ""
    return max(candidates, key=lambda item: (_report_day(item[0]) or date.min, 1 if item[1] == "远端" else 0))


def _policy_snapshot(
    row: dict[str, Any],
    weights: dict[str, float],
    *,
    source: str,
    today: date,
    max_age: int,
    horizon: str,
) -> AttributionPolicySnapshot:
    report_day = _report_day(row)
    shadow = row.get("shadow_diff_stats_json") if isinstance(row.get("shadow_diff_stats_json"), dict) else {}
    governor = _policy_governor(row)
    execution = attribution_execution_state(governor, _recommendation_rows(row.get("recommendations_json")))
    operations = attribution_operations_brief(shadow, execution)
    formal_allowed = attribution_formal_dynamic_allowed(governor)
    checklist = _promotion_checklist(governor)
    return AttributionPolicySnapshot(
        weights=weights,
        source=source,
        report_date=report_day.isoformat() if report_day else "",
        horizon=str(horizon or "5"),
        age_days=(today - report_day).days if report_day else None,
        max_age_days=max_age,
        governor_status=str(governor.get("status") or ""),
        mode_recommendation=str(governor.get("mode_recommendation") or ""),
        next_action=str(governor.get("next_action") or ""),
        next_action_summary=str(governor.get("next_action_summary") or ""),
        auto_apply=bool(governor.get("auto_apply")),
        execution_policy=str(execution.get("funnel_dynamic_policy") or ""),
        execution_scope=str(execution.get("scope") or ""),
        formal_dynamic_allowed=formal_allowed,
        formal_dynamic_block_reason=str(execution.get("formal_dynamic_block_reason") or ""),
        promotion_checklist_summary=_checklist_summary(checklist),
        backtest_confirmation_text=_backtest_confirmation_text(checklist),
        signal_action_count=int(execution.get("signal_action_count") or 0),
        selection_action_count=int(execution.get("selection_action_count") or 0),
        selection_action_summary=str(operations.get("selection_action_summary") or ""),
        execution_summary=str(execution.get("summary") or ""),
    )


def _snapshot_log_text(snapshot: AttributionPolicySnapshot) -> str:
    weights = format_policy_weight_text(snapshot.weights, limit=12, delimiter=", ")
    return f"{format_policy_meta_text(snapshot.as_dict())}; {weights}"


def _active_scope_text(*, shadow_active: bool, formal_active: bool) -> str:
    if formal_active:
        return "正式漏斗"
    if shadow_active:
        return "漏斗shadow"
    return "无"


def _policy_governor(row: dict[str, Any]) -> dict[str, Any]:
    shadow = row.get("shadow_diff_stats_json")
    if not isinstance(shadow, dict):
        return {}
    governor = shadow.get("policy_governor")
    return governor if isinstance(governor, dict) else {}


def _recommendation_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _promotion_checklist(governor: dict[str, Any]) -> list[dict[str, str]]:
    rows = governor.get("promotion_checklist")
    if not isinstance(rows, list):
        return []
    return [
        {
            "key": str(row.get("key") or ""),
            "status": str(row.get("status") or "unknown"),
            "summary": str(row.get("summary") or "-"),
        }
        for row in rows
        if isinstance(row, dict)
    ]


def _checklist_summary(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    return "；".join(
        f"{_checklist_label(row.get('key', ''))}={_status_label(row.get('status') or 'unknown')}" for row in rows
    )


def _backtest_confirmation_text(rows: list[dict[str, str]]) -> str:
    for row in rows:
        if row.get("key") == "backtest_confirmation":
            return f"{_status_label(row.get('status', 'unknown'))}({row.get('summary') or '-'})"
    return ""


def _checklist_label(key: str) -> str:
    labels = {
        "shadow_sample": "样本",
        "shadow_added_outperforms_removed": "新增跑赢",
        "selection_actions": "候选源治理",
        "signal_actions": "信号调权",
        "backtest_confirmation": "回测",
    }
    return labels.get(key, key or "-")


def _status_label(status: str) -> str:
    labels = {
        "pass": "通过",
        "fail": "失败",
        "review": "待复核",
        "missing": "缺失",
        "not_required": "不需要",
        "unknown": "未知",
    }
    return labels.get(status, status or "未知")


def _fresh_report(
    row: dict[str, Any] | None,
    *,
    today: date,
    log_fn: Callable[[str], None] | None,
    source: str,
) -> dict[str, Any] | None:
    if not row:
        return None
    max_age = _max_report_age_days()
    if max_age <= 0:
        return row
    report_day = _report_day(row)
    if report_day is None:
        _log(log_fn, f"策略归因调权: {source}报告缺少 report_date，跳过。")
        return None
    age = (today - report_day).days
    if age < 0 or age > max_age:
        _log(log_fn, f"策略归因调权: {source}报告已过期(report_date={report_day}, age={age}d)，跳过。")
        return None
    return row


def _report_day(row: dict[str, Any]) -> date | None:
    raw = row.get("report_date") or row.get("created_at")
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _max_report_age_days() -> int:
    raw = os.getenv("STRATEGY_ATTRIBUTION_MAX_AGE_DAYS", "7")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 7


def _normalize_funnel_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in {"off", "shadow", "on"} else "off"


def _log(log_fn: Callable[[str], None] | None, message: str) -> None:
    if log_fn:
        log_fn(message)
