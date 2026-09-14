"""Strategy attribution report workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from core.constants import TABLE_STRATEGY_ATTRIBUTION_REPORTS
from core.strategy_policy_display import (
    format_policy_signal_label,
    policy_execution_display,
    policy_execution_mode_label,
    policy_governor_display,
    policy_mode_recommendation_label,
    policy_next_action_label,
    policy_promotion_check_key_label,
    policy_promotion_check_status_label,
    policy_promotion_status_label,
)
from integrations.supabase_base import (
    close_client,
    create_admin_client,
    create_read_client,
    require_server_write_context,
)
from workflows.strategy_attribution_execution import attribution_execution_state, attribution_operations_brief
from workflows.strategy_attribution_stats import build_strategy_attribution_payload


@dataclass(frozen=True)
class StrategyAttributionRequest:
    market: str = "cn"
    days: int = 30
    horizons: tuple[int, ...] = (1, 3, 5, 10, 20)
    output_dir: Path | None = None
    no_write: bool = False
    backtest_confirmation_json: dict[str, Any] | None = None
    formal_dynamic_approval_json: dict[str, Any] | None = None


def parse_horizons(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in str(raw or "").split(",") if x.strip())


def run_strategy_attribution_report(request: StrategyAttributionRequest) -> dict[str, Any]:
    client = create_report_client(no_write=request.no_write)
    try:
        report = build_report(
            client,
            request.market,
            request.days,
            list(request.horizons),
            backtest_confirmation_json=request.backtest_confirmation_json,
            formal_dynamic_approval_json=request.formal_dynamic_approval_json,
        )
        attach_policy_execution_state(report)
        report["created_at"] = datetime.now(UTC).isoformat()
        if not request.no_write:
            write_report(client, report)
        if request.output_dir:
            write_artifacts(report, request.output_dir)
        return report
    finally:
        close_client(client)


def build_console_summary(report: dict[str, Any], *, written: bool) -> dict[str, Any]:
    governor = _report_policy_governor(report)
    shadow = report.get("shadow_diff_stats_json") or {}
    execution = _report_policy_execution_state(report)
    operations = _report_policy_operations(report)
    return {
        "market": report.get("market"),
        "report_date": report.get("report_date"),
        "written": written,
        "policy_status": governor.get("status", "unknown"),
        "mode_recommendation": governor.get("mode_recommendation", "keep_shadow"),
        "next_action": governor.get("next_action", "keep_shadow_observe"),
        "next_action_summary": governor.get("next_action_summary", "-"),
        "promotion_status": governor.get("promotion_status", "unknown"),
        "auto_apply": bool(governor.get("auto_apply")),
        "policy_summary": governor.get("summary", "-"),
        "policy_display": policy_governor_display(governor),
        "shadow_runs": shadow.get("count", 0) if isinstance(shadow, dict) else 0,
        "execution_policy": execution.get("funnel_dynamic_policy", "off"),
        "execution_horizon": execution.get("horizon", "5"),
        "execution_scope": execution.get("scope", "none"),
        "execution_summary": policy_execution_display(execution),
        "formal_dynamic_allowed": bool(execution.get("formal_dynamic_allowed")),
        "formal_dynamic_block_reason": execution.get("formal_dynamic_block_reason", ""),
        "active_scope": execution.get("active_scope", "无"),
        "funnel_shadow_weights_active": bool(execution.get("funnel_shadow_weights_active")),
        "funnel_formal_weights_active": bool(execution.get("funnel_formal_weights_active")),
        "signal_action_count": execution.get("signal_action_count", 0),
        "selection_action_count": execution.get("selection_action_count", 0),
        "selection_action_summary": operations.get("selection_action_summary", "候选源治理=无"),
        "promotion_checklist_summary": operations.get("promotion_checklist_summary", "-"),
        "promotion_blockers": operations.get("promotion_blockers", []),
        "backtest_confirmation": operations.get("backtest_confirmation", {}),
        "operator_summary": operations.get("operator_summary", "-"),
    }


def build_report(
    client: Any,
    market: str,
    days: int,
    horizons: list[int],
    *,
    backtest_confirmation_json: dict[str, Any] | None = None,
    formal_dynamic_approval_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    end = date.today()
    start = end - timedelta(days=days)
    observations = fetch_all(client, "signal_observations", "*", market=market, start=start, end=end)
    outcomes = fetch_all(client, "signal_outcomes", "*", market=market, start=start, end=end)
    shadow = fetch_all(client, "signal_policy_shadow_runs", "*", market=market, start=start, end=end)
    return build_strategy_attribution_payload(
        report_date=end,
        market=market,
        window_start=start,
        window_end=end,
        horizons=horizons,
        observations=observations,
        outcomes=outcomes,
        shadow_runs=shadow,
        backtest_confirmation_json=backtest_confirmation_json,
        formal_dynamic_approval_json=formal_dynamic_approval_json,
    )


def fetch_all(client: Any, table: str, select: str, *, market: str, start: date, end: date) -> list[dict[str, Any]]:
    date_col = "trade_date" if table != "strategy_attribution_reports" else "report_date"
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = _fetch_page(
            client, table, select, market=market, start=start, end=end, date_col=date_col, offset=offset
        )
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        offset += 1000


# 落库瘦身。报告是**每交易日一行**，单行从 2026-07-02 的 99KB 涨到 2026-09-11 的 870KB，
# 按 245 交易日/年算约 208MB/年，是全库最大的字节增长源。涨的是四个 JSON 列，而它们里面
# 有相当一部分写完就没有读取方——同一批 action 在一行里存了三份，粒度递减：
#   governor.{signal,context,selection}_actions  168KB  全 5 档  写后无人读
#   recommendations_json                         223KB  全 5 档  读取方只用焦点档
#   shadow_diff.policy_execution_state            38KB  仅焦点档 chat-tools 优先读它
# 第三份才是消费者要的形状。下面把前两份收敛到同一口径，只在**落库时**裁，
# write_artifacts 写的 report.json 仍是全量，所以排查证据不丢（artifact 活 90 天），
# 且这些派生量都能由 signal_observations/signal_outcomes 重算（两表留存 365 天）。

# governor 的三个 action 列表：_selection_actions/_context_actions/_signal_actions 的产物，
# 已被 governor_recommendation_rows 拍平进 recommendations_json。落库后读 governor 的
# 三处（history_tools._policy_governor_record、attribution.tsx 的 policyGovernor、
# strategy_attribution_policy._policy_governor）都只取标量键和 promotion_checklist。
_GOVERNOR_ACTION_KEYS = ("signal_actions", "context_actions", "selection_actions")

# score_bucket_stats_json 里这四个桶只在写时被 core.strategy_policy_governor._selection_actions
# 消费；web 侧四个 flatten 函数读的是 _candidate_shadow_grade/_entry_quality_grade/
# _data_lineage/_observation_coverage。用「丢弃名单」而非「保留名单」，
# 这样 web 以后加新桶不会被这里静默吃掉。
_STORAGE_DROP_SCORE_BUCKETS = ("_selection_mode", "_strategy_version", "_candidate_lane", "_entry_type")


def slim_report_for_storage(report: dict[str, Any]) -> dict[str, Any]:
    """返回落库用的瘦身副本，不改动入参（artifact 随后还要写全量）。"""
    slim = dict(report)
    # 写后无读取方，且可由 observations+outcomes 重算。显式写 None 而不是省略键，
    # 这样同一 report_date 重跑时能把历史上那些胖行一并清掉。
    slim["signal_context_stats_json"] = None
    slim["score_bucket_stats_json"] = _slim_score_buckets(report.get("score_bucket_stats_json"))
    shadow = report.get("shadow_diff_stats_json")
    horizon = ""
    if isinstance(shadow, dict):
        governor = shadow.get("policy_governor")
        if isinstance(governor, dict):
            horizon = str(governor.get("horizon") or "")
            slim_shadow = dict(shadow)
            slim_shadow["policy_governor"] = {
                key: value for key, value in governor.items() if key not in _GOVERNOR_ACTION_KEYS
            }
            slim["shadow_diff_stats_json"] = slim_shadow
    slim["recommendations_json"] = _slim_recommendations(report.get("recommendations_json"), horizon)
    return slim


def _slim_score_buckets(score_buckets: Any) -> Any:
    if not isinstance(score_buckets, dict):
        return score_buckets
    return {key: value for key, value in score_buckets.items() if key not in _STORAGE_DROP_SCORE_BUCKETS}


def _slim_recommendations(rows: Any, horizon: str) -> Any:
    """只留焦点档。所有读取方本来就按 governor.horizon 过滤，故非焦点档落库即死数据。

    signal_weight_multipliers_from_rows 传的 horizon 来自 policy_horizon(row)，
    attribution_execution_state 用 governor["horizon"]，attribution.tsx 的
    policyExecutionStats 用 executionPayload.horizon——三者都是同一个焦点档。
    horizon 拿不到时不裁，避免把整列清空。
    """
    if not isinstance(rows, list) or not horizon:
        return rows
    return [
        row
        for row in rows
        if not isinstance(row, dict) or row.get("type") == "policy_governor" or str(row.get("horizon") or "") == horizon
    ]


def write_report(client: Any, report: dict[str, Any]) -> None:
    client.table(TABLE_STRATEGY_ATTRIBUTION_REPORTS).upsert(
        slim_report_for_storage(report),
        on_conflict="report_date,market,window_start,window_end",
    ).execute()


def write_artifacts(report: dict[str, Any], output_dir: Path) -> None:
    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(build_report_markdown(report) + "\n", encoding="utf-8")


def build_report_markdown(report: dict[str, Any]) -> str:
    shadow = report.get("shadow_diff_stats_json") or {}
    shadow_h5 = ((shadow.get("outcome_stats") or {}).get("5") or {}) if isinstance(shadow, dict) else {}
    governor = _report_policy_governor(report)
    execution = _report_policy_execution_state(report)
    lines = [
        f"# 策略归因报告 {report['report_date']}",
        "",
        f"- 市场: `{report['market']}`",
        f"- 窗口: `{report['window_start']}` 至 `{report['window_end']}`",
        "",
        "## Shadow 差异",
        f"- run 数: `{shadow.get('count', 0) if isinstance(shadow, dict) else 0}`",
        f"- 平均新增: `{shadow.get('avg_added', 0) if isinstance(shadow, dict) else 0}`",
        f"- 平均移除: `{shadow.get('avg_removed', 0) if isinstance(shadow, dict) else 0}`",
        f"- h=5 新增组: `{json.dumps(shadow_h5.get('added') or {}, ensure_ascii=False)}`",
        f"- h=5 移除组: `{json.dumps(shadow_h5.get('removed') or {}, ensure_ascii=False)}`",
        "",
        "## 运营复盘",
        *_operations_markdown_lines(report),
        "",
        "## 策略治理",
        f"- 状态: {_governor_status_text(governor)}",
        f"- 动态策略建议: {_mode_recommendation_text(governor)}",
        f"- 下一步动作: {_next_action_text(governor)}",
        f"- 下一步说明: {governor.get('next_action_summary', '-') if isinstance(governor, dict) else '-'}",
        f"- 晋级状态: {_promotion_status_text(governor)}",
        f"- 自动生效: {policy_governor_display(governor).get('auto_apply', '否')}",
        f"- 摘要: {governor.get('summary', '-') if isinstance(governor, dict) else '-'}",
        "",
        "### 晋级检查",
        *_promotion_checklist_lines(governor),
        "",
        "## 调权执行状态",
        f"- 漏斗动态策略: {policy_execution_mode_label(execution.get('funnel_dynamic_policy', 'off'))}",
        f"- 执行周期: `h={execution.get('horizon', '5')}`",
        f"- 当前生效范围: `{execution.get('active_scope', '无')}`",
        f"- 底层范围: {_execution_scope_text(execution)}",
        f"- 可执行调权: `{execution.get('signal_action_count', 0)}`",
        f"- 候选源治理: `{execution.get('selection_action_count', 0)}`",
        f"- 摘要: {execution.get('summary', '暂无可执行信号调权。')}",
        "",
        "## 治理建议",
    ]
    lines.extend(_recommendation_markdown_rows(report.get("recommendations_json") or []))
    return "\n".join(lines)


def _governor_status_text(governor: dict[str, Any]) -> str:
    raw = _governor_value(governor, "status", "unknown")
    return _label_with_raw(policy_governor_display(governor).get("status", "未知"), raw)


def _mode_recommendation_text(governor: dict[str, Any]) -> str:
    raw = _governor_value(governor, "mode_recommendation", "keep_shadow")
    return _label_with_raw(policy_mode_recommendation_label(raw), raw)


def _next_action_text(governor: dict[str, Any]) -> str:
    raw = _governor_value(governor, "next_action", "keep_shadow_observe")
    return _label_with_raw(policy_next_action_label(raw), raw)


def _promotion_status_text(governor: dict[str, Any]) -> str:
    raw = _governor_value(governor, "promotion_status", "unknown")
    return _label_with_raw(policy_promotion_status_label(raw), raw)


def _governor_value(governor: dict[str, Any], key: str, default: str) -> str:
    if not isinstance(governor, dict):
        return default
    return str(governor.get(key) or default).strip()


def _label_with_raw(label: str, raw: str) -> str:
    return f"{label} (`{raw}`)"


def _operations_markdown_lines(report: dict[str, Any]) -> list[str]:
    shadow = report.get("shadow_diff_stats_json") or {}
    latest = shadow.get("latest") if isinstance(shadow, dict) else {}
    execution = _report_policy_execution_state(report)
    operations = _report_policy_operations(report)
    lines = [f"- 操作摘要: {operations.get('operator_summary', '-')}"]
    lines.extend(_latest_shadow_lines(latest if isinstance(latest, dict) else {}))
    lines.extend(_action_detail_lines(execution.get("action_details") if isinstance(execution, dict) else []))
    return lines or ["- 暂无可用运营复盘信息。"]


def _promotion_checklist_lines(governor: dict[str, Any]) -> list[str]:
    rows = governor.get("promotion_checklist") if isinstance(governor, dict) else []
    if not isinstance(rows, list) or not rows:
        return ["- 暂无晋级检查清单。"]
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "-")
        status = str(row.get("status") or "unknown")
        lines.append(
            f"- {policy_promotion_check_key_label(key)} (`{key}`): "
            f"{policy_promotion_check_status_label(status)} (`{status}`) — {row.get('summary', '-')}"
        )
    return lines or ["- 暂无晋级检查清单。"]


def _execution_scope_text(execution: dict[str, Any]) -> str:
    raw = str(execution.get("scope") or "none").strip()
    active = str(execution.get("active_scope") or "无").strip()
    return f"{active} (`{raw}`)" if raw and raw != "none" else active


def _latest_shadow_lines(latest: dict[str, Any]) -> list[str]:
    if not latest:
        return ["- 最新 shadow: 暂无。"]
    selection = latest.get("selection_summary") if isinstance(latest.get("selection_summary"), dict) else {}
    added_sample = ", ".join(str(x) for x in latest.get("diff_added_sample") or []) or "-"
    removed_sample = ", ".join(str(x) for x in latest.get("diff_removed_sample") or []) or "-"
    return [
        (
            f"- 最新 shadow: `{latest.get('trade_date', '-')}` / `{latest.get('regime', '-')}`，"
            f"base `{selection.get('base_count', '-')}` -> shadow `{selection.get('shadow_count', '-')}`，"
            f"新增 `{selection.get('diff_added_count', '-')}`，移除 `{selection.get('diff_removed_count', '-')}`，"
            f"Jaccard `{selection.get('jaccard', '-')}`"
        ),
        f"- Shadow 新增样本: `{added_sample}`",
        f"- Shadow 移除样本: `{removed_sample}`",
    ]


def _action_detail_lines(raw: Any) -> list[str]:
    rows = raw if isinstance(raw, list) else []
    if not rows:
        return ["- 本期可执行调权: 无。"]
    lines = ["- 本期可执行调权:"]
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        lines.append(
            f"  - `{row.get('label', row.get('target', '-'))}` {row.get('action', '-')}"
            f" ×{row.get('weight_multiplier', 1.0)}；"
            f"avg={evidence.get('avg_return_pct', '-')}，win={evidence.get('win_rate_pct', '-')}%，"
            f"dd={evidence.get('avg_drawdown_pct', '-')}"
        )
    if len(rows) > 8:
        lines.append(f"  - ... 另 {len(rows) - 8} 项")
    return lines


def _report_policy_governor(report: dict[str, Any]) -> dict[str, Any]:
    shadow = report.get("shadow_diff_stats_json") or {}
    if not isinstance(shadow, dict):
        return {}
    governor = shadow.get("policy_governor")
    return governor if isinstance(governor, dict) else {}


def attach_policy_execution_state(report: dict[str, Any]) -> None:
    shadow = report.get("shadow_diff_stats_json")
    if not isinstance(shadow, dict):
        return
    execution = attribution_execution_state(
        _report_policy_governor(report), list(report.get("recommendations_json") or [])
    )
    shadow["policy_execution_state"] = execution
    shadow["policy_operations_brief"] = attribution_operations_brief(shadow, execution)


def _report_policy_execution_state(report: dict[str, Any]) -> dict[str, Any]:
    shadow = report.get("shadow_diff_stats_json") or {}
    if isinstance(shadow, dict) and isinstance(shadow.get("policy_execution_state"), dict):
        return shadow["policy_execution_state"]
    return attribution_execution_state(_report_policy_governor(report), list(report.get("recommendations_json") or []))


def _report_policy_operations(report: dict[str, Any]) -> dict[str, Any]:
    shadow = report.get("shadow_diff_stats_json") or {}
    if isinstance(shadow, dict) and isinstance(shadow.get("policy_operations_brief"), dict):
        return shadow["policy_operations_brief"]
    return attribution_operations_brief(
        shadow if isinstance(shadow, dict) else {}, _report_policy_execution_state(report)
    )


def _recommendation_markdown_rows(rows: list[dict[str, Any]]) -> list[str]:
    rendered = []
    for row in rows:
        if row.get("type") == "policy_governor":
            continue
        payload = _json_payload(row.get("reason"))
        action = str(row.get("type") or payload.get("action") or "watch")
        weight = payload.get("weight_multiplier", "-")
        evidence = payload.get("evidence") or {}
        target = format_policy_signal_label(row.get("target") or payload.get("target"), payload.get("scope") or {})
        rendered.append(
            f"- `{target}` h={row.get('horizon')}: {action}, weight={weight}, "
            f"avg={evidence.get('avg_return_pct', '-')}, win={evidence.get('win_rate_pct', '-')}%, "
            f"dd={evidence.get('avg_drawdown_pct', '-')}"
        )
    return rendered or ["- 暂无需要调整的信号。"]


def _json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def create_report_client(*, no_write: bool) -> Any:
    if no_write:
        return create_user_read_client() or create_read_client()
    require_server_write_context("write strategy_attribution_reports")
    return create_admin_client()


def create_user_read_client() -> Any | None:
    try:
        from integrations.local_auth import restore_session
        from integrations.supabase_base import create_user_client

        session = restore_session()
        if session and session.get("access_token"):
            return create_user_client(session["access_token"], session.get("refresh_token", ""))
    except Exception:
        return None
    return None


def _fetch_page(
    client: Any,
    table: str,
    select: str,
    *,
    market: str,
    start: date,
    end: date,
    date_col: str,
    offset: int,
) -> list[dict[str, Any]]:
    query = (
        client.table(table)
        .select(select)
        .eq("market", market)
        .gte(date_col, start.isoformat())
        .lte(date_col, end.isoformat())
        .range(offset, offset + 999)
    )
    return query.execute().data or []
