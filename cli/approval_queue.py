"""待批准写操作队列 — daemon 无人时把高风险调用存下来等人决定。"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".wyckoff" / "approvals.db"

# 隔夜批准会按旧价成交，所以过期不是清理策略而是安全要求。
DEFAULT_TTL_HOURS = 12

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
EXECUTED = "executed"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    schedule_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_at TEXT NOT NULL DEFAULT '',
    executed_at TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
"""


@dataclass
class PendingApproval:
    id: str
    created_at: str
    source: str
    schedule_id: str
    tool_name: str
    args_json: str
    summary: str
    risk: str
    status: str
    decided_at: str = ""
    executed_at: str = ""
    result_json: str = ""

    @property
    def args(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.args_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def is_expired(self, *, ttl_hours: int = DEFAULT_TTL_HOURS, now: datetime | None = None) -> bool:
        created = _parse(self.created_at)
        if created is None:
            return False
        return (now or _utcnow()) - created > timedelta(hours=ttl_hours)


def enqueue(
    tool_name: str,
    args: dict[str, Any],
    *,
    risk: str,
    source: str,
    schedule_id: str = "",
    summary: str = "",
    db_path: Path | None = None,
) -> str:
    approval_id = uuid.uuid4().hex[:10]
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO approvals (id, created_at, source, schedule_id, tool_name,"
            " args_json, summary, risk, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                approval_id,
                _utcnow().isoformat(timespec="seconds"),
                source,
                schedule_id,
                tool_name,
                json.dumps(args, ensure_ascii=False, default=str),
                summary,
                risk,
                PENDING,
            ),
        )
    return approval_id


def list_pending(*, db_path: Path | None = None, ttl_hours: int = DEFAULT_TTL_HOURS) -> list[PendingApproval]:
    """返回未过期的待批项；顺带把已过期的标记掉。"""
    expire_stale(ttl_hours=ttl_hours, db_path=db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, created_at, source, schedule_id, tool_name, args_json,"
            " summary, risk, status, decided_at, executed_at, result_json FROM approvals"
            " WHERE status = ? ORDER BY created_at",
            (PENDING,),
        ).fetchall()
    return [PendingApproval(*row) for row in rows]


def get(approval_id: str, *, db_path: Path | None = None) -> PendingApproval | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, created_at, source, schedule_id, tool_name, args_json,"
            " summary, risk, status, decided_at, executed_at, result_json FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
    return PendingApproval(*row) if row else None


def decide(
    approval_id: str,
    *,
    approved: bool,
    db_path: Path | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> PendingApproval | None:
    """批准或拒绝。已过期或已决策的项返回 None，不允许翻案。"""
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, created_at, source, schedule_id, tool_name, args_json,"
            " summary, risk, status, decided_at, executed_at, result_json FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        record = PendingApproval(*row) if row else None
        if record is None or record.status != PENDING:
            return None
        decided_at = _utcnow().isoformat(timespec="seconds")
        if record.is_expired(ttl_hours=ttl_hours):
            conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
                (EXPIRED, decided_at, approval_id, PENDING),
            )
            return None
        status = APPROVED if approved else REJECTED
        changed = conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
            (status, decided_at, approval_id, PENDING),
        ).rowcount
        if not changed:
            return None
        record.status = status
        record.decided_at = decided_at
        return record


def expire_stale(*, ttl_hours: int = DEFAULT_TTL_HOURS, db_path: Path | None = None) -> int:
    cutoff = (_utcnow() - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE status = ? AND created_at < ?",
            (EXPIRED, _utcnow().isoformat(timespec="seconds"), PENDING, cutoff),
        )
        return cur.rowcount or 0


def record_execution(
    approval_id: str,
    result: Any,
    *,
    succeeded: bool,
    db_path: Path | None = None,
) -> None:
    status = EXECUTED if succeeded else FAILED
    payload = json.dumps(result, ensure_ascii=False, default=str)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE approvals SET status = ?, executed_at = ?, result_json = ? WHERE id = ? AND status = ?",
            (status, _utcnow().isoformat(timespec="seconds"), payload, approval_id, APPROVED),
        )


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    if "executed_at" not in columns:
        conn.execute("ALTER TABLE approvals ADD COLUMN executed_at TEXT NOT NULL DEFAULT ''")
    if "result_json" not in columns:
        conn.execute("ALTER TABLE approvals ADD COLUMN result_json TEXT NOT NULL DEFAULT ''")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def summarize(tool_name: str, args: dict[str, Any]) -> str:
    """给人读的一行摘要，写进队列供 CLI 和 UI 展示。"""
    code = args.get("code") or ""
    name = args.get("name") or ""
    label = f"{code} {name}".strip() or tool_name
    if tool_name == "set_stop_loss":
        items = args.get("items")
        if isinstance(items, list) and items:
            return f"批量补止损 {len(items)} 只"
        if args.get("stop_loss") is not None:
            return f"{label} 止损 → {args['stop_loss']}"
    action = args.get("side") if tool_name == "record_trade_fill" else args.get("action")
    action = action or ""
    shares = args.get("shares")
    if action and shares is not None:
        return f"{label} {action} {shares} 股"
    return label


_SENSITIVE_KEYS = ("token", "secret", "password", "cookie", "api_key", "private_key", "authorization")


def sanitized_args(args: dict[str, Any]) -> dict[str, Any]:
    def clean(value: Any, key: str = "") -> Any:
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            return "***"
        if isinstance(value, dict):
            return {str(k): clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(args)


__all__ = [
    "APPROVED",
    "DEFAULT_TTL_HOURS",
    "EXPIRED",
    "EXECUTED",
    "FAILED",
    "PENDING",
    "REJECTED",
    "PendingApproval",
    "decide",
    "enqueue",
    "expire_stale",
    "get",
    "list_pending",
    "record_execution",
    "sanitized_args",
    "summarize",
]
