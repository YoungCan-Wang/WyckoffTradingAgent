"""Supabase signal_pending 表读写，模式同 supabase_recommendation.py。

去重键必须与库里的唯一约束同源。生产约束 ``uq_signal_pending_active`` 建在
``(code, signal_type)`` 上、只作用于活跃态（pending/survived），**与 signal_date 无关**；
而写入前的去重一度只按 ``(code, signal_type, signal_date)`` 且只查当天，看不见跨日仍活跃
的行。2026-08-31 因此整批写失败：912/sos 在 8-28 还是 survived，当天 140 条触发信号被
PostgREST 的单语句批量 insert 一起回滚，signal_pending 里 8-31 一行都没有（8-28 有 38 行、
8-27 有 60 行）。日志只有一行 ``write pending signals failed``，外层 except 吞掉后返回 0。

所以这里做两件事：按约束自己的键（活跃态 code+signal_type）过滤，以及批量撞约束时退化为
逐行插入——让残留竞态只吃掉冲突那一行，而不是一整天的信号。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.constants import TABLE_SIGNAL_PENDING
from integrations.supabase_base import create_admin_client as _admin
from integrations.supabase_base import create_read_client as _read
from integrations.supabase_base import is_admin_configured as _configured
from integrations.supabase_base import require_server_write_context

logger = logging.getLogger(__name__)

_OPTIONAL_REPORT_COLUMNS = {"candidate_theme", "candidate_phase", "candidate_role"}
# 与库里 uq_signal_pending_active 的作用范围一致：只有活跃态占用唯一键。
ACTIVE_SIGNAL_STATUSES = ("pending", "survived")


def insert_pending_signal_rows(rows: list[dict[str, Any]]) -> int:
    """Insert new pending signal rows, skipping already-pending duplicates."""
    if not _configured() or not rows:
        return 0
    require_server_write_context("write signal_pending")

    try:
        client = _admin()
        to_insert = _rows_not_yet_active(client, rows)
        if not to_insert:
            logger.info("%s pending signals already exist; skipped", len(rows))
            return 0
        written = _insert_with_fallbacks(client, to_insert)
        logger.info(
            "inserted %s pending signals; skipped %s existing, %s conflicted",
            written,
            len(rows) - len(to_insert),
            len(to_insert) - written,
        )
        return written
    except Exception as e:
        logger.warning("write pending signals failed: %s", e)
        return 0


def _active_key(row: dict[str, Any]) -> tuple[int, str]:
    return int(row["code"]), row["signal_type"]


def _day_key(row: dict[str, Any]) -> tuple[int, str, str]:
    return int(row["code"]), row["signal_type"], str(row.get("signal_date") or "")


def _rows_not_yet_active(client: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """两道过滤，缺一不可。

    1. 活跃态的 ``(code, signal_type)``——这是库里 ``uq_signal_pending_active`` 的键。
       不能只查当天：8-28 的 survived 行同样占用唯一键，跨日照样撞。
    2. 当天的 ``(code, signal_type, signal_date)``，不分状态——一天内跑第二遍时，
       已 confirmed/expired 的当天信号不该被重新挂成 pending。这是原有语义，保留。

    同一批入参里重复的键也要去掉，否则批量 insert 会自己跟自己冲突。
    """
    signal_dates = sorted({str(row.get("signal_date") or "") for row in rows if row.get("signal_date")})
    active = (
        client.table(TABLE_SIGNAL_PENDING)
        .select("code,signal_type")
        .in_("status", list(ACTIVE_SIGNAL_STATUSES))
        .execute()
    )
    taken_active = {_active_key(r) for r in (active.data or [])}
    taken_day: set[tuple[int, str, str]] = set()
    if signal_dates:
        same_day = (
            client.table(TABLE_SIGNAL_PENDING).select("code,signal_type,signal_date").in_("signal_date", signal_dates)
        ).execute()
        taken_day = {_day_key(r) for r in (same_day.data or [])}
    deduped: list[dict[str, Any]] = []
    for row in rows:
        if _active_key(row) in taken_active or _day_key(row) in taken_day:
            continue
        taken_active.add(_active_key(row))
        taken_day.add(_day_key(row))
        deduped.append(row)
    return deduped


def _insert_with_fallbacks(client: Any, to_insert: list[dict[str, Any]]) -> int:
    """批量插入；撞唯一约束时退化为逐行，避免一行冲突回滚一整天的信号。"""
    try:
        client.table(TABLE_SIGNAL_PENDING).insert(to_insert).execute()
        return len(to_insert)
    except Exception as exc:
        if _looks_like_schema_miss(exc):
            legacy_rows = [
                {key: value for key, value in row.items() if key not in _OPTIONAL_REPORT_COLUMNS} for row in to_insert
            ]
            client.table(TABLE_SIGNAL_PENDING).insert(legacy_rows).execute()
            logger.warning("signal_pending report columns missing; wrote compatible payload")
            return len(legacy_rows)
        if not _looks_like_unique_conflict(exc):
            raise
        logger.warning("signal_pending batch hit unique conflict; retrying row by row: %s", exc)
        return _insert_row_by_row(client, to_insert)


def _insert_row_by_row(client: Any, to_insert: list[dict[str, Any]]) -> int:
    written = 0
    for row in to_insert:
        try:
            client.table(TABLE_SIGNAL_PENDING).insert([row]).execute()
            written += 1
        except Exception as exc:
            if not _looks_like_unique_conflict(exc):
                raise
            logger.info("signal_pending skip conflicting row code=%s type=%s", row.get("code"), row.get("signal_type"))
    return written


def _looks_like_schema_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return "column" in text or "schema cache" in text or "could not find" in text


def _looks_like_unique_conflict(exc: Exception) -> bool:
    """PostgREST 把唯一约束冲突报成 409 / SQLSTATE 23505。"""
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text or "already exists" in text


def load_pending_signals() -> list[dict[str, Any]]:
    try:
        return (
            _read().table(TABLE_SIGNAL_PENDING).select("*").in_("status", ["pending", "survived"]).execute().data or []
        )
    except Exception as e:
        logger.warning("load pending signals failed: %s", e)
        return []


def batch_update_signals(updates: list[dict[str, Any]]) -> bool:
    if not _configured() or not updates:
        return True
    require_server_write_context("update signal_pending")
    try:
        client = _admin()
        now_iso = datetime.now(UTC).isoformat()
        for upd in updates:
            row_id = upd.get("id")
            if row_id is None:
                continue
            row: dict[str, Any] = {
                "status": upd["status"],
                "days_elapsed": upd.get("days_elapsed", 0),
                "confirm_reason": upd.get("confirm_reason", ""),
                "updated_at": now_iso,
            }
            if upd.get("confirm_date"):
                row["confirm_date"] = upd["confirm_date"]
            if upd.get("expire_date"):
                row["expire_date"] = upd["expire_date"]
            client.table(TABLE_SIGNAL_PENDING).update(row).eq("id", row_id).execute()
        logger.info("updated %s pending signals", len(updates))
        return True
    except Exception as e:
        logger.warning("update pending signals failed: %s", e)
        return False
