"""Supabase signal_pending 表读写，模式同 supabase_recommendation.py。"""

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


def insert_pending_signal_rows(rows: list[dict[str, Any]]) -> int:
    """Insert new pending signal rows, skipping already-pending duplicates."""
    if not _configured() or not rows:
        return 0
    require_server_write_context("write signal_pending")

    try:
        client = _admin()
        existing = client.table(TABLE_SIGNAL_PENDING).select("code,signal_type").eq("status", "pending").execute()
        existing_keys = {(int(r["code"]), r["signal_type"]) for r in (existing.data or [])}
        to_insert = [row for row in rows if (int(row["code"]), row["signal_type"]) not in existing_keys]
        if not to_insert:
            logger.info("%s pending signals already exist; skipped", len(rows))
            return 0
        try:
            client.table(TABLE_SIGNAL_PENDING).insert(to_insert).execute()
        except Exception as exc:
            if not _looks_like_schema_miss(exc):
                raise
            legacy_rows = [
                {key: value for key, value in row.items() if key not in _OPTIONAL_REPORT_COLUMNS} for row in to_insert
            ]
            client.table(TABLE_SIGNAL_PENDING).insert(legacy_rows).execute()
            logger.warning("signal_pending report columns missing; wrote compatible payload")
        logger.info("inserted %s pending signals; skipped %s existing", len(to_insert), len(rows) - len(to_insert))
        return len(to_insert)
    except Exception as e:
        logger.warning("write pending signals failed: %s", e)
        return 0


def _looks_like_schema_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return "column" in text or "schema cache" in text or "could not find" in text


def load_pending_signals() -> list[dict[str, Any]]:
    try:
        return _read().table(TABLE_SIGNAL_PENDING).select("*").eq("status", "pending").execute().data or []
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
