from __future__ import annotations

import json
import logging

from agents.tool_context import ToolContext, get_user_client
from core.pattern_review.records import pattern_review_tool_records
from core.strategy_policy_display import (
    format_policy_signal_label,
    policy_execution_display,
    policy_governor_display,
)
from utils.json_text import parse_json_object as _json_map

logger = logging.getLogger(__name__)


def query_history(
    source: str,
    status: str = "all",
    limit: int = 20,
    query: str = "",
    archive_ref: str = "",
    tool_context: ToolContext | None = None,
    market: str = "cn",
) -> dict:
    source = (source or "").strip().lower()
    if source == "recommendation":
        # market 只对 recommendation 有意义：归因是全局的，signal/archive 不分市场。
        return _query_recommendation(limit, tool_context, market=market)
    if source == "signal":
        return _query_signal(status, limit, tool_context)
    if source == "archive":
        return _query_archive(query, archive_ref, limit, tool_context)
    if source == "attribution":
        return _query_attribution(limit, tool_context)
    return {"error": f"不支持的 source：{source}，请用 'recommendation'、'signal'、'attribution' 或 'archive'"}


def _query_recommendation(limit: int, tool_context: ToolContext | None = None, market: str = "cn") -> dict:
    """
    某个市场的推荐跟踪。

    只有 A 股走「先本地缓存」：本地 SQLite 的 recommendation_tracking 只缓存
    A 股，美股/港股读它会把 A 股的行摆到美股页签下面。宁可多一次网络往返，
    也不能张冠李戴。
    """
    try:
        limit = min(max(int(limit), 1), 200)
        normalized = str(market or "cn").strip().lower()
        from integrations.supabase_recommendation import RECOMMENDATION_TABLES

        # 回显规范化后的市场：认不出的值会退回 cn 去查，那就得说自己是 cn，
        # 否则界面会把 A 股的行标成用户点的那个市场。
        if normalized not in RECOMMENDATION_TABLES:
            normalized = "cn"
        records = _load_local_recommendations(limit) if normalized == "cn" else []
        if not records:
            records = _load_remote_recommendations(limit, tool_context, market=normalized)
        if not records:
            return {"message": "暂无复盘记录", "records": [], "market": normalized}
        simplified = pattern_review_tool_records(records)
        return {"total": len(simplified), "records": simplified, "market": normalized}
    except Exception as e:
        logger.exception("query_history(recommendation) error")
        return {"error": str(e)}


def _load_local_recommendations(limit: int) -> list[dict]:
    try:
        from integrations.local_db import load_recommendations

        return load_recommendations(limit=limit) or []
    except Exception:
        logger.warning("failed to load recommendations from local DB", exc_info=True)
        return []


def _load_remote_recommendations(
    limit: int,
    tool_context: ToolContext | None,
    market: str = "cn",
) -> list[dict]:
    from integrations.supabase_recommendation import load_recommendation_tracking

    records = (
        load_recommendation_tracking(limit=limit, client=get_user_client(tool_context), market=market) or []
    )
    # 只回写 A 股：本地那张表就是 A 股的缓存，把美股行塞进去会污染 A 股结果，
    # 而且下次 A 股走本地优先时会读到美股的票。
    if records and str(market or "cn").strip().lower() == "cn":
        _cache_recommendations(records)
    return records


def _cache_recommendations(records: list[dict]) -> None:
    try:
        from integrations.local_db import save_recommendations

        save_recommendations(records)
    except Exception:
        logger.warning("failed to cache recommendations locally", exc_info=True)


def _query_signal(status: str, limit: int, tool_context: ToolContext | None = None) -> dict:
    try:
        limit = min(max(int(limit), 1), 100)
        rows = _load_signal_rows(status, limit, tool_context)
        if not rows:
            label = {"pending": "待确认", "confirmed": "已确认", "expired": "已过期"}.get(status, "")
            return {"message": f"暂无{label}信号记录", "records": []}
        records = [_signal_record(row) for row in rows]
        return {"total": len(records), "status_counts": _status_counts(records), "records": records}
    except Exception as e:
        logger.exception("query_history(signal) error")
        return {"error": str(e)}


def _load_signal_rows(status: str, limit: int, tool_context: ToolContext | None) -> list[dict]:
    rows = _load_local_signal_rows(status, limit)
    if rows:
        return rows
    rows = _load_remote_signal_rows(status, limit, tool_context)
    if rows:
        _cache_signals(rows)
    return rows


def _load_local_signal_rows(status: str, limit: int) -> list[dict]:
    try:
        from integrations.local_db import load_signals

        normalized_status = status if status in ("pending", "confirmed", "expired") else None
        return load_signals(status=normalized_status, limit=limit) or []
    except Exception:
        logger.warning("failed to load signals from local DB", exc_info=True)
        return []


def _load_remote_signal_rows(status: str, limit: int, tool_context: ToolContext | None) -> list[dict]:
    from core.constants import TABLE_SIGNAL_PENDING
    from integrations.supabase_base import create_read_client

    client = get_user_client(tool_context) or create_read_client()
    query = client.table(TABLE_SIGNAL_PENDING).select("*")
    if status in ("pending", "confirmed", "expired"):
        query = query.eq("status", status)
    return query.order("updated_at", desc=True).limit(limit).execute().data or []


def _cache_signals(rows: list[dict]) -> None:
    try:
        from integrations.local_db import save_signals

        save_signals(rows)
    except Exception:
        logger.warning("failed to cache signals locally", exc_info=True)


def _signal_record(row: dict) -> dict:
    return {
        "code": _signal_code(row.get("code", "")),
        "name": str(row.get("name", "")),
        "signal_type": str(row.get("signal_type", "")),
        "signal_date": str(row.get("signal_date", "")),
        "status": str(row.get("status", "")),
        "days_elapsed": row.get("days_elapsed", 0),
        "ttl_days": row.get("ttl_days", 3),
        "signal_score": row.get("signal_score", 0),
        "snap_close": row.get("snap_close"),
        "confirm_date": str(row.get("confirm_date", "") or ""),
        "expire_date": str(row.get("expire_date", "") or ""),
        "confirm_reason": str(row.get("confirm_reason", "") or ""),
        "regime": str(row.get("regime", "") or ""),
        "industry": str(row.get("industry", "") or ""),
    }


def _signal_code(value) -> str:
    try:
        return f"{int(value):06d}"
    except Exception:
        return str(value or "").strip()


def _status_counts(records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = record["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def _query_attribution(limit: int, tool_context: ToolContext | None = None) -> dict:
    try:
        rows = _load_attribution_rows(min(max(int(limit), 1), 10), tool_context)
        if not rows:
            return {"message": "暂无策略归因报告", "records": []}
        records = [_attribution_record(row) for row in rows]
        return {
            "total": len(records),
            "latest_source": records[0].get("source", "remote"),
            "remote_error": records[0].get("remote_error", ""),
            "latest_policy": records[0].get("policy_governor", {}),
            "latest_policy_display": records[0].get("policy_display", {}),
            "latest_execution_state": records[0].get("execution_state", {}),
            "latest_execution_summary": records[0].get("execution_summary", {}),
            "latest_operations": records[0].get("operations", {}),
            "latest_operator_summary": records[0].get("operations", {}).get("operator_summary", ""),
            "records": records,
        }
    except Exception as e:
        logger.exception("query_history(attribution) error")
        return {"error": str(e)}


def _load_attribution_rows(limit: int, tool_context: ToolContext | None) -> list[dict]:
    rows: list[dict] = []
    remote_error: Exception | None = None
    try:
        rows.extend(
            _with_attribution_source(row, "remote") for row in _load_remote_attribution_rows(limit, tool_context)
        )
    except Exception as exc:
        logger.warning("failed to load attribution reports from remote", exc_info=True)
        remote_error = exc

    local = _load_local_attribution_row()
    if local:
        rows.append(_with_attribution_source(local, "local", remote_error))
    if rows:
        return _sort_attribution_rows(rows)[:limit]
    if remote_error is not None:
        raise remote_error
    return []


def _load_remote_attribution_rows(limit: int, tool_context: ToolContext | None) -> list[dict]:
    from core.constants import TABLE_STRATEGY_ATTRIBUTION_REPORTS
    from integrations.supabase_base import create_read_client

    client = get_user_client(tool_context) or create_read_client()
    return (
        client.table(TABLE_STRATEGY_ATTRIBUTION_REPORTS)
        .select("*")
        .eq("market", "cn")
        .order("report_date", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )


def _load_local_attribution_row() -> dict | None:
    from workflows.strategy_attribution_policy import load_local_attribution_report

    return load_local_attribution_report("cn")


def _with_attribution_source(row: dict, source: str, remote_error: Exception | None = None) -> dict:
    annotated = dict(row)
    annotated["_source"] = source
    if remote_error:
        annotated["_remote_error"] = str(remote_error)
    return annotated


def _sort_attribution_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_attribution_sort_key, reverse=True)


def _attribution_sort_key(row: dict) -> tuple[str, int, str]:
    source_rank = 1 if row.get("_source") == "remote" else 0
    return (str(row.get("report_date") or ""), source_rank, str(row.get("created_at") or ""))


def _attribution_record(row: dict) -> dict:
    from workflows.strategy_attribution_execution import attribution_operations_brief

    shadow = _json_map(row.get("shadow_diff_stats_json"))
    governor = _json_map(shadow.get("policy_governor"))
    actions = _attribution_actions(row.get("recommendations_json"))
    governor_record = _policy_governor_record(governor)
    execution = _attribution_execution_state(governor_record, actions)
    policy_display = policy_governor_display(governor_record)
    execution_summary = policy_execution_display(execution)
    return {
        "source": str(row.get("_source") or "remote"),
        "remote_error": str(row.get("_remote_error") or ""),
        "report_date": str(row.get("report_date", "")),
        "window_start": str(row.get("window_start", "")),
        "window_end": str(row.get("window_end", "")),
        "policy_governor": governor_record,
        "policy_display": policy_display,
        "execution_state": execution,
        "execution_summary": execution_summary,
        "operations": attribution_operations_brief(shadow, execution),
        "signal_actions": actions,
        "shadow": {
            "runs": shadow.get("count", 0),
            "avg_added": shadow.get("avg_added", 0),
            "avg_removed": shadow.get("avg_removed", 0),
        },
    }


def _policy_governor_record(governor: dict) -> dict:
    checklist = governor.get("promotion_checklist")
    return {
        "status": str(governor.get("status", "unknown")),
        "mode_recommendation": str(governor.get("mode_recommendation", "keep_shadow")),
        "next_action": str(governor.get("next_action", "keep_shadow_observe")),
        "next_action_summary": str(governor.get("next_action_summary", "-")),
        "promotion_status": str(governor.get("promotion_status", "unknown")),
        "promotion_checklist": checklist if isinstance(checklist, list) else [],
        "auto_apply": bool(governor.get("auto_apply")),
        "formal_dynamic_allowed": governor.get("formal_dynamic_allowed"),
        "formal_dynamic_approval": str(governor.get("formal_dynamic_approval", "")),
        "formal_dynamic_block_reason": str(governor.get("formal_dynamic_block_reason", "")),
        "summary": str(governor.get("summary", "-")),
        "horizon": str(governor.get("horizon", "")),
    }


def _attribution_execution_state(governor: dict, actions: list[dict]) -> dict:
    from workflows.strategy_attribution_execution import attribution_execution_state

    return attribution_execution_state(governor, actions)


def _attribution_actions(raw: object) -> list[dict]:
    rows = []
    for row in _json_list(raw):
        if not isinstance(row, dict) or row.get("type") == "policy_governor":
            continue
        payload = _json_map(row.get("reason"))
        scope = _json_map(payload.get("scope"))
        target = str(row.get("target") or payload.get("target") or "")
        rows.append(
            {
                "action": str(row.get("type") or payload.get("action") or ""),
                "horizon": str(row.get("horizon") or payload.get("horizon") or ""),
                "target": target,
                "label": format_policy_signal_label(target, scope),
                "weight_multiplier": payload.get("weight_multiplier"),
                "scope": scope,
                "evidence": _json_map(payload.get("evidence")),
            }
        )
    return rows[:12]


def _json_list(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _query_archive(
    query: str = "",
    archive_ref: str = "",
    limit: int = 5,
    tool_context: ToolContext | None = None,
) -> dict:
    try:
        from utils.context_archive import restore_context_archive, search_context_archives

        if archive_ref:
            return _restore_archive(archive_ref, restore_context_archive)
        if not query:
            return {"error": "查询归档时必须提供 query 或 archive_ref 参数"}
        return _search_archive(query, limit, tool_context, search_context_archives)
    except Exception as exc:
        logger.exception("query_history(archive) error")
        return {"error": str(exc)}


def _archive_session_id(tool_context: ToolContext | None) -> str:
    if tool_context and tool_context.state:
        return tool_context.state.get("session_id", "")
    return ""


def _restore_archive(archive_ref: str, restore_context_archive) -> dict:
    records = restore_context_archive(archive_ref)
    if not records:
        return {"error": f"未找到或无法还原归档: {archive_ref}"}
    messages = [_archive_message(row) for row in records]
    return {"archive_ref": archive_ref, "message_count": len(messages), "messages": messages}


def _archive_message(row: dict) -> dict:
    message = row.get("message", {})
    return {"role": message.get("role"), "content": message.get("content"), "name": message.get("name")}


def _search_archive(query: str, limit: int, tool_context: ToolContext | None, search_context_archives) -> dict:
    results = search_context_archives(query, session_id=_archive_session_id(tool_context), limit=limit)
    if not results:
        return {"message": f"未找到与 '{query}' 相关的历史对话归档", "results": []}
    simplified = [
        {
            "archive_ref": row.get("archive_ref"),
            "created_at": row.get("created_at"),
            "summary": row.get("summary"),
            "codes": row.get("codes"),
            "message_count": row.get("message_count"),
        }
        for row in results
    ]
    return {"query": query, "total": len(simplified), "results": simplified}
