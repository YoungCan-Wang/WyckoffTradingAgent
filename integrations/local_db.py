"""
本地 SQLite 存储层 — CLI Agent 的离线缓存 + 记忆。

所有 CLI 场景下的读操作优先走本地 SQLite，Supabase 降级为 fallback。
GitHub Actions 不用此模块。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from core import constants as core_constants
from core.candidate_metadata import CANDIDATE_ATTRIBUTION_COLUMNS
from utils.safe import finite_float, safe_float

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA_VERSION = 13

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recommendation_tracking (
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    recommend_date INTEGER NOT NULL,
    recommend_reason TEXT DEFAULT '',
    initial_price REAL DEFAULT 0,
    current_price REAL DEFAULT 0,
    change_pct REAL DEFAULT 0,
    funnel_score REAL,
    recommend_count INTEGER DEFAULT 0,
    is_ai_recommended INTEGER DEFAULT 0,
    rag_vetoed INTEGER DEFAULT 0,
    camp TEXT DEFAULT '',
    selection_source TEXT DEFAULT '',
    selection_rank INTEGER,
    priority_score REAL,
    trigger_score REAL,
    capital_migration_bonus REAL,
    stage TEXT DEFAULT '',
    industry TEXT DEFAULT '',
    strategy_version TEXT DEFAULT '',
    candidate_lane TEXT DEFAULT '',
    entry_type TEXT DEFAULT '',
    signal_key TEXT DEFAULT '',
    candidate_status TEXT DEFAULT '',
    candidate_timing TEXT DEFAULT '',
    candidate_risk TEXT DEFAULT '',
    candidate_reasons TEXT DEFAULT '',
    candidate_metrics TEXT DEFAULT '',
    mainline_score REAL,
    theme_score REAL,
    stock_role_score REAL,
    quality_score REAL,
    timing_score REAL,
    synced_at TEXT DEFAULT (datetime('now')),
    UNIQUE(code, recommend_date)
);

CREATE TABLE IF NOT EXISTS signal_pending (
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    signal_type TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    signal_score REAL DEFAULT 0,
    days_elapsed INTEGER DEFAULT 0,
    regime TEXT DEFAULT '',
    industry TEXT DEFAULT '',
    snap_support REAL,
    snap_ma20 REAL,
    strategy_version TEXT DEFAULT '',
    candidate_lane TEXT DEFAULT '',
    entry_type TEXT DEFAULT '',
    signal_key TEXT DEFAULT '',
    candidate_status TEXT DEFAULT '',
    candidate_timing TEXT DEFAULT '',
    candidate_risk TEXT DEFAULT '',
    candidate_reasons TEXT DEFAULT '',
    candidate_metrics TEXT DEFAULT '',
    mainline_score REAL,
    theme_score REAL,
    stock_role_score REAL,
    quality_score REAL,
    timing_score REAL,
    synced_at TEXT DEFAULT (datetime('now')),
    UNIQUE(code, signal_type, signal_date)
);

CREATE TABLE IF NOT EXISTS market_signal_daily (
    trade_date TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    synced_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio (
    portfolio_id TEXT PRIMARY KEY,
    free_cash REAL DEFAULT 0,
    synced_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_position (
    portfolio_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    shares INTEGER DEFAULT 0,
    cost_price REAL DEFAULT 0,
    buy_dt TEXT DEFAULT '',
    stop_loss REAL,
    synced_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (portfolio_id, code)
);

CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    codes TEXT DEFAULT '',
    memory_level TEXT DEFAULT 'L1',
    source_ref TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_meta (
    table_name TEXT PRIMARY KEY,
    last_synced_at TEXT NOT NULL,
    row_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    elapsed_s REAL DEFAULT 0,
    error TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '',
    metadata TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tail_buy_history (
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    run_date TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    signal_type TEXT DEFAULT '',
    status TEXT DEFAULT '',
    final_decision TEXT NOT NULL,
    rule_decision TEXT DEFAULT '',
    rule_score REAL DEFAULT 0,
    priority_score REAL DEFAULT 0,
    rule_reasons TEXT DEFAULT '',
    llm_decision TEXT DEFAULT '',
    llm_reason TEXT DEFAULT '',
    llm_confidence REAL,
    llm_model_used TEXT DEFAULT '',
    initial_price REAL DEFAULT 0,
    current_price REAL DEFAULT 0,
    change_pct REAL DEFAULT 0,
    price_updated_at TEXT DEFAULT '',
    last_close REAL DEFAULT 0,
    vwap REAL DEFAULT 0,
    dist_vwap_pct REAL DEFAULT 0,
    close_pos REAL DEFAULT 0,
    day_ret_pct REAL DEFAULT 0,
    last30_ret_pct REAL DEFAULT 0,
    last15_ret_pct REAL DEFAULT 0,
    tail30_volume_share REAL DEFAULT 0,
    drop_from_high_pct REAL DEFAULT 0,
    fetch_error TEXT DEFAULT '',
    features_json TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(code, run_date)
);

CREATE TABLE IF NOT EXISTS background_task_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT DEFAULT '',
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    result_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id)
);

CREATE TABLE IF NOT EXISTS theme_radar_snapshot (
    trade_date TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    synced_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_run (
    run_id TEXT PRIMARY KEY,
    session_id TEXT DEFAULT '',
    workflow TEXT NOT NULL,
    label TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    user_text TEXT DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '{}',
    current_step INTEGER DEFAULT 0,
    result_summary TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendation_tracking(recommend_date);
CREATE INDEX IF NOT EXISTS idx_sig_status ON signal_pending(status);
CREATE INDEX IF NOT EXISTS idx_mem_type ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_codes ON agent_memory(codes);
CREATE INDEX IF NOT EXISTS idx_chatlog_session ON chat_log(session_id);
CREATE INDEX IF NOT EXISTS idx_chatlog_created ON chat_log(created_at);
CREATE INDEX IF NOT EXISTS idx_tail_run_date ON tail_buy_history(run_date);
CREATE INDEX IF NOT EXISTS idx_tail_decision ON tail_buy_history(final_decision);
CREATE INDEX IF NOT EXISTS idx_bg_task_session ON background_task_result(session_id);
CREATE INDEX IF NOT EXISTS idx_bg_task_created ON background_task_result(created_at);
CREATE INDEX IF NOT EXISTS idx_theme_radar_synced ON theme_radar_snapshot(synced_at);
CREATE INDEX IF NOT EXISTS idx_workflow_run_session ON workflow_run(session_id);
CREATE INDEX IF NOT EXISTS idx_workflow_run_updated ON workflow_run(updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_event_run ON workflow_event(run_id, id);

-- FTS5 全文检索索引（记忆系统 hybrid search）
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
    content,
    content=agent_memory,
    content_rowid=id,
    tokenize='unicode61'
);

-- 保持 FTS5 与 agent_memory 同步的触发器
CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        db_path = core_constants.LOCAL_DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        _conn = conn
        return _conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(_DDL)
    _ensure_recommendation_tracking_columns(conn)
    _ensure_signal_pending_columns(conn)
    _ensure_agent_memory_columns(conn)
    _ensure_tail_buy_history_columns(conn)
    cur = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current = row[0] if row and row[0] else 0
    if current < 4:
        try:
            conn.execute("ALTER TABLE portfolio_position ADD COLUMN buy_dt TEXT DEFAULT ''")
        except Exception:
            logger.warning("migration: add buy_dt column failed", exc_info=True)
    if current < 5:
        _backfill_background_tasks_from_chat_log(conn)
    if current < 6:
        _migrate_fts5_memory(conn)
    if current < 7:
        try:
            conn.execute("ALTER TABLE chat_log ADD COLUMN metadata TEXT DEFAULT ''")
        except Exception:
            logger.warning("migration: add metadata column failed", exc_info=True)
    if current < 8:
        _ensure_agent_memory_columns(conn)
    if current < 9:
        _ensure_tail_buy_history_columns(conn)
    if current < 13:
        _ensure_recommendation_tracking_columns(conn)
        _ensure_signal_pending_columns(conn)
    if current < _SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version) VALUES(?)",
            (_SCHEMA_VERSION,),
        )
        conn.commit()


def _ensure_recommendation_tracking_columns(conn: sqlite3.Connection) -> None:
    columns = {
        "change_pct": "REAL DEFAULT 0",
        "funnel_score": "REAL",
        "recommend_count": "INTEGER DEFAULT 0",
        "selection_source": "TEXT DEFAULT ''",
        "selection_rank": "INTEGER",
        "priority_score": "REAL",
        "trigger_score": "REAL",
        "capital_migration_bonus": "REAL",
        "stage": "TEXT DEFAULT ''",
        "industry": "TEXT DEFAULT ''",
        **_candidate_sqlite_columns(),
    }
    _ensure_columns(conn, "recommendation_tracking", columns)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_lane ON recommendation_tracking(candidate_lane)")


def _ensure_signal_pending_columns(conn: sqlite3.Connection) -> None:
    columns = {
        "snap_support": "REAL",
        "snap_ma20": "REAL",
        **_candidate_sqlite_columns(),
    }
    _ensure_columns(conn, "signal_pending", columns)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sig_lane ON signal_pending(candidate_lane)")


def _candidate_sqlite_columns() -> dict[str, str]:
    json_columns = {"candidate_reasons", "candidate_metrics"}
    real_columns = {"mainline_score", "theme_score", "stock_role_score", "quality_score", "timing_score"}
    return {
        column: "REAL" if column in real_columns else "TEXT DEFAULT ''"
        for column in CANDIDATE_ATTRIBUTION_COLUMNS
        if column not in json_columns
    } | {column: "TEXT DEFAULT ''" for column in json_columns}


_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add missing columns to *table*. Identifiers are whitelist-validated before
    being interpolated into DDL, since sqlite3 cannot bind table/column names as
    parameters.
    """
    if not _SQL_IDENTIFIER_RE.match(table):
        raise ValueError(f"unsafe table identifier: {table!r}")
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if not _SQL_IDENTIFIER_RE.match(name):
            raise ValueError(f"unsafe column identifier: {name!r}")
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _bulk_upsert(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: list[dict],
    value_fn: Any,
    *,
    timestamp_column: str = "synced_at",
) -> int:
    """INSERT OR REPLACE *rows* into *table*, appending `datetime('now')` as the last column."""
    if not rows:
        return 0
    if not _SQL_IDENTIFIER_RE.match(table):
        raise ValueError(f"unsafe table identifier: {table!r}")
    placeholders = ", ".join("?" for _ in columns)
    with conn:
        conn.executemany(
            f"""INSERT OR REPLACE INTO {table}
               ({", ".join(columns)}, {timestamp_column})
               VALUES ({placeholders}, datetime('now'))""",
            [value_fn(r) for r in rows],
        )
    return len(rows)


def _bulk_delete_by_codes(conn: sqlite3.Connection, table: str, codes: list[str]) -> int:
    if not codes:
        return 0
    if not _SQL_IDENTIFIER_RE.match(table):
        raise ValueError(f"unsafe table identifier: {table!r}")
    placeholders = ",".join("?" for _ in codes)
    with conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE code IN ({placeholders})", codes)
    return cur.rowcount


def _ensure_agent_memory_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "agent_memory",
        {
            "memory_level": "TEXT DEFAULT 'L1'",
            "source_ref": "TEXT DEFAULT ''",
            "confidence": "REAL DEFAULT 1.0",
            "metadata": "TEXT DEFAULT ''",
        },
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_level ON agent_memory(memory_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_source ON agent_memory(source_ref)")


def _ensure_tail_buy_history_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "tail_buy_history",
        {
            "rule_decision": "TEXT DEFAULT ''",
            "llm_confidence": "REAL",
            "llm_model_used": "TEXT DEFAULT ''",
            "initial_price": "REAL DEFAULT 0",
            "current_price": "REAL DEFAULT 0",
            "change_pct": "REAL DEFAULT 0",
            "price_updated_at": "TEXT DEFAULT ''",
            "last_close": "REAL DEFAULT 0",
            "vwap": "REAL DEFAULT 0",
            "dist_vwap_pct": "REAL DEFAULT 0",
            "close_pos": "REAL DEFAULT 0",
            "day_ret_pct": "REAL DEFAULT 0",
            "last30_ret_pct": "REAL DEFAULT 0",
            "last15_ret_pct": "REAL DEFAULT 0",
            "tail30_volume_share": "REAL DEFAULT 0",
            "drop_from_high_pct": "REAL DEFAULT 0",
            "fetch_error": "TEXT DEFAULT ''",
            "features_json": "TEXT DEFAULT ''",
        },
    )


def _backfill_background_tasks_from_chat_log(conn: sqlite3.Connection) -> None:
    """Backfill historical background completions that were only saved as chat messages."""
    try:
        rows = conn.execute(
            """SELECT id, session_id, content, created_at
               FROM chat_log
               WHERE role='user' AND content LIKE '[后台任务完成] %'"""
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        content = str(row["content"] or "")
        rest = content.removeprefix("[后台任务完成] ").strip()
        tool_name = rest.split(":", 1)[0].strip() or "background"
        status = "failed" if '"error"' in content or "'error'" in content else "completed"
        payload = {"raw": content}
        if ":" in rest:
            raw_json = rest.split(":", 1)[1].strip()
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                payload = {"raw": raw_json}
        result_json = json.dumps(payload, ensure_ascii=False, default=str)
        summary = background_task_result_summary(tool_name, f"chatlog_{row['id']}", payload, result_json)
        conn.execute(
            """INSERT OR IGNORE INTO background_task_result
               (task_id, session_id, tool_name, status, result_json, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"chatlog_{row['id']}",
                row["session_id"] or "",
                tool_name,
                status,
                result_json,
                summary,
                row["created_at"],
            ),
        )


def _migrate_fts5_memory(conn: sqlite3.Connection) -> None:
    """为已有 agent_memory 数据创建 FTS5 索引。"""
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
                content, content=agent_memory, content_rowid=id, tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON agent_memory BEGIN
                INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON agent_memory BEGIN
                INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE ON agent_memory BEGIN
                INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
                INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        # 回填已有数据
        rows = conn.execute("SELECT id, content FROM agent_memory").fetchall()
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO agent_memory_fts(rowid, content) VALUES (?, ?)",
                    (row["id"], row["content"]),
                )
            except Exception:
                logger.warning("fts5 backfill row failed", exc_info=True)
    except Exception:
        logger.warning("fts5 memory migration failed", exc_info=True)


def _recommendation_local_values(row: dict) -> tuple:
    base = (
        str(row.get("code", "")).strip(),
        str(row.get("name", "")).strip(),
        int(row.get("recommend_date", 0) or 0),
        str(row.get("recommend_reason", "")).strip(),
        safe_float(row.get("initial_price")),
        safe_float(row.get("current_price")),
        safe_float(row.get("change_pct")),
        finite_float(row.get("funnel_score")),
        int(row.get("recommend_count", 0) or 0),
        1 if row.get("is_ai_recommended") else 0,
        str(row.get("camp", "")).strip(),
        str(row.get("selection_source", "")).strip(),
        _nullable_int(row.get("selection_rank")),
        finite_float(row.get("priority_score")),
        finite_float(row.get("trigger_score")),
        finite_float(row.get("capital_migration_bonus")),
        str(row.get("stage", "")).strip(),
        str(row.get("industry", "")).strip(),
    )
    return base + _candidate_local_values(row)


def _signal_local_values(row: dict) -> tuple:
    base = (
        str(row.get("code", "")).strip(),
        str(row.get("name", "")).strip(),
        str(row.get("signal_type", "")).strip(),
        str(row.get("signal_date", "")).strip(),
        str(row.get("status", "pending")).strip(),
        safe_float(row.get("signal_score")),
        int(row.get("days_elapsed", 0) or 0),
        str(row.get("regime", "")).strip(),
        str(row.get("industry", "")).strip(),
        finite_float(row.get("snap_support")),
        finite_float(row.get("snap_ma20")),
    )
    return base + _candidate_local_values(row)


def _candidate_local_values(row: dict) -> tuple:
    return tuple(_candidate_local_value(column, row.get(column)) for column in CANDIDATE_ATTRIBUTION_COLUMNS)


def _candidate_local_value(column: str, value: Any) -> Any:
    if column in {"candidate_reasons", "candidate_metrics"}:
        return _json_text(value)
    if column in {"mainline_score", "theme_score", "stock_role_score", "quality_score", "timing_score"}:
        return finite_float(value)
    return str(value or "").strip()


def _json_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _nullable_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Recommendation tracking
# ---------------------------------------------------------------------------


def save_recommendations(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_db()
    columns = [
        "code",
        "name",
        "recommend_date",
        "recommend_reason",
        "initial_price",
        "current_price",
        "change_pct",
        "funnel_score",
        "recommend_count",
        "is_ai_recommended",
        "camp",
        "selection_source",
        "selection_rank",
        "priority_score",
        "trigger_score",
        "capital_migration_bonus",
        "stage",
        "industry",
        *CANDIDATE_ATTRIBUTION_COLUMNS,
    ]
    return _bulk_upsert(conn, "recommendation_tracking", columns, rows, _recommendation_local_values)


def load_recommendations(*, limit: int = 100) -> list[dict]:
    conn = get_db()
    cur = conn.execute(
        "SELECT * FROM recommendation_tracking ORDER BY recommend_date DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Signal pending
# ---------------------------------------------------------------------------


def save_signals(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_db()
    columns = [
        "code",
        "name",
        "signal_type",
        "signal_date",
        "status",
        "signal_score",
        "days_elapsed",
        "regime",
        "industry",
        "snap_support",
        "snap_ma20",
        *CANDIDATE_ATTRIBUTION_COLUMNS,
    ]
    return _bulk_upsert(conn, "signal_pending", columns, rows, _signal_local_values)


def delete_recommendations(codes: list[str]) -> int:
    return _bulk_delete_by_codes(get_db(), "recommendation_tracking", codes)


def load_signals(*, status: str | None = None, limit: int = 200) -> list[dict]:
    conn = get_db()
    try:
        if status:
            cur = conn.execute(
                "SELECT * FROM signal_pending WHERE status=? ORDER BY signal_date DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM signal_pending ORDER BY signal_date DESC LIMIT ?",
                (limit,),
            )
    except sqlite3.OperationalError as exc:
        if "no such table: signal_pending" in str(exc).lower():
            logger.info("local signal_pending table is unavailable; returning empty signal cache")
            return []
        raise
    return [dict(r) for r in cur.fetchall()]


def load_signals_by_codes(codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    conn = get_db()
    ph = ",".join("?" for _ in codes)
    try:
        cur = conn.execute(
            f"SELECT * FROM signal_pending WHERE code IN ({ph}) ORDER BY signal_date DESC",
            codes,
        )
    except sqlite3.OperationalError as exc:
        if "no such table: signal_pending" in str(exc).lower():
            logger.info("local signal_pending table is unavailable; returning empty signal cache")
            return {}
        raise
    result: dict[str, dict] = {}
    for r in cur.fetchall():
        row = dict(r)
        code = row.get("code", "")
        if code not in result:
            result[code] = row
    return result


def delete_signals(codes: list[str]) -> int:
    return _bulk_delete_by_codes(get_db(), "signal_pending", codes)


# ---------------------------------------------------------------------------
# Market signal daily
# ---------------------------------------------------------------------------


def save_market_signal(trade_date: str, data: dict) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_signal_daily
               (trade_date, data_json, synced_at) VALUES (?, ?, datetime('now'))""",
            (str(trade_date).strip(), json.dumps(data, ensure_ascii=False)),
        )


def load_latest_market_signal() -> dict | None:
    conn = get_db()
    cur = conn.execute("SELECT data_json FROM market_signal_daily ORDER BY trade_date DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Theme radar snapshot
# ---------------------------------------------------------------------------


def save_theme_radar_snapshot(snapshot: dict[str, Any]) -> None:
    trade_date = str(snapshot.get("trade_date", "") or "").strip()
    if not trade_date:
        raise ValueError("theme radar snapshot requires trade_date")
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO theme_radar_snapshot
               (trade_date, snapshot_json, synced_at) VALUES (?, ?, datetime('now'))""",
            (trade_date, json.dumps(snapshot, ensure_ascii=False, default=str)),
        )


def load_latest_theme_radar_snapshot() -> dict | None:
    conn = get_db()
    try:
        cur = conn.execute("SELECT snapshot_json FROM theme_radar_snapshot ORDER BY trade_date DESC LIMIT 1")
    except sqlite3.OperationalError as exc:
        if "no such table: theme_radar_snapshot" in str(exc).lower():
            return None
        raise
    row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["snapshot_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


def save_portfolio(portfolio_id: str, free_cash: float, positions: list[dict]) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO portfolio
               (portfolio_id, free_cash, synced_at) VALUES (?, ?, datetime('now'))""",
            (portfolio_id, free_cash),
        )
        conn.execute(
            "DELETE FROM portfolio_position WHERE portfolio_id=?",
            (portfolio_id,),
        )
        if positions:
            conn.executemany(
                """INSERT INTO portfolio_position
                   (portfolio_id, code, name, shares, cost_price, buy_dt, stop_loss, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [
                    (
                        portfolio_id,
                        str(p.get("code", "")).strip(),
                        str(p.get("name", "")).strip(),
                        int(p.get("shares", 0) or 0),
                        float(p.get("cost_price", 0) or 0),
                        str(p.get("buy_dt", "") or ""),
                        float(p["stop_loss"]) if p.get("stop_loss") is not None else None,
                    )
                    for p in positions
                ],
            )


def load_portfolio(portfolio_id: str) -> dict | None:
    conn = get_db()
    cur = conn.execute("SELECT * FROM portfolio WHERE portfolio_id=?", (portfolio_id,))
    row = cur.fetchone()
    if not row:
        return None
    pos_cur = conn.execute("SELECT * FROM portfolio_position WHERE portfolio_id=?", (portfolio_id,))
    return {
        "portfolio_id": row["portfolio_id"],
        "free_cash": row["free_cash"],
        "positions": [dict(p) for p in pos_cur.fetchall()],
    }


def _ensure_local_portfolio(portfolio_id: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO portfolio (portfolio_id, free_cash) VALUES (?, 0)",
        (portfolio_id,),
    )
    conn.commit()


def upsert_local_position(
    portfolio_id: str,
    code: str,
    name: str,
    shares: int,
    cost_price: float,
    buy_dt: str = "",
) -> None:
    _ensure_local_portfolio(portfolio_id)
    conn = get_db()
    with conn:
        if buy_dt:
            conn.execute(
                """INSERT INTO portfolio_position
                   (portfolio_id, code, name, shares, cost_price, buy_dt, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(portfolio_id, code) DO UPDATE SET
                   name=excluded.name, shares=excluded.shares,
                   cost_price=excluded.cost_price, buy_dt=excluded.buy_dt,
                   synced_at=excluded.synced_at""",
                (portfolio_id, code, name, shares, cost_price, buy_dt),
            )
        else:
            conn.execute(
                """INSERT INTO portfolio_position
                   (portfolio_id, code, name, shares, cost_price, synced_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(portfolio_id, code) DO UPDATE SET
                   name=excluded.name, shares=excluded.shares,
                   cost_price=excluded.cost_price,
                   synced_at=excluded.synced_at""",
                (portfolio_id, code, name, shares, cost_price),
            )


def delete_local_position(portfolio_id: str, code: str) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            "DELETE FROM portfolio_position WHERE portfolio_id=? AND code=?",
            (portfolio_id, code),
        )


def update_local_free_cash(portfolio_id: str, free_cash: float) -> None:
    _ensure_local_portfolio(portfolio_id)
    conn = get_db()
    with conn:
        conn.execute(
            "UPDATE portfolio SET free_cash=?, synced_at=datetime('now') WHERE portfolio_id=?",
            (free_cash, portfolio_id),
        )


# ---------------------------------------------------------------------------
# Agent memory
# ---------------------------------------------------------------------------


_MEMORY_KEEP_LIMITS: dict[str, int] = {
    "preference": 50,
    "persona": 5,
    "playbook": 20,
    "scenario": 20,
    "session": 50,
    "fact": 50,
    "stock_opinion": 30,
    "decision": 30,
    "market_view": 20,
}
_MEMORY_RECALL_WEIGHTS = {
    "fts": 0.8,
    "code": 1.2,
    "keyword": 0.25,
}
_MEMORY_DECAY_HALF_LIFE_DAYS = {
    "decision": 14.0,
    "playbook": 21.0,
    "scenario": 21.0,
    "stock_opinion": 14.0,
    "market_view": 14.0,
    "fact": 14.0,
}
_MEMORY_RETENTION_DAYS = {
    "decision": 45,
    "playbook": 60,
    "scenario": 60,
    "stock_opinion": 45,
    "market_view": 30,
    "fact": 45,
    "session": 30,
}
_MEMORY_NO_DECAY_TYPES = {"preference", "persona"}


def _memory_level(memory_type: str) -> str:
    if memory_type == "persona":
        return "L3"
    if memory_type in {"playbook", "scenario"}:
        return "L2"
    return "L1"


def _memory_metadata_text(metadata: dict[str, Any] | str | None) -> str:
    if metadata is None:
        return ""
    if isinstance(metadata, str):
        return metadata
    return json.dumps(metadata, ensure_ascii=False, default=str)


def save_memory(
    memory_type: str,
    content: str,
    codes: str = "",
    *,
    memory_level: str | None = None,
    source_ref: str = "",
    confidence: float = 1.0,
    metadata: dict[str, Any] | str | None = None,
) -> int:
    content = str(content).strip()
    if not content:
        return 0
    conn = get_db()
    level = memory_level or _memory_level(memory_type)
    metadata_text = _memory_metadata_text(metadata)
    with conn:
        existing = conn.execute(
            """SELECT id FROM agent_memory
               WHERE memory_type=? AND content=? AND codes=?
               ORDER BY created_at DESC LIMIT 1""",
            (memory_type, content, codes),
        ).fetchone()
        if existing:
            if source_ref or metadata_text:
                conn.execute(
                    """UPDATE agent_memory
                       SET source_ref = CASE WHEN ?!='' AND source_ref='' THEN ? ELSE source_ref END,
                           metadata = CASE WHEN ?!='' THEN ? ELSE metadata END
                       WHERE id=?""",
                    (source_ref, source_ref, metadata_text, metadata_text, existing["id"]),
                )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO agent_memory
               (memory_type, content, codes, memory_level, source_ref, confidence, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memory_type, content, codes, level, source_ref, confidence, metadata_text),
        )
        limit = _MEMORY_KEEP_LIMITS.get(memory_type, 50)
        conn.execute(
            """DELETE FROM agent_memory WHERE memory_type = ? AND id NOT IN (
                   SELECT id FROM agent_memory WHERE memory_type = ?
                   ORDER BY created_at DESC LIMIT ?
               )""",
            (memory_type, memory_type, limit),
        )
        return cur.lastrowid or 0


def get_memory_by_id(memory_id: int) -> dict | None:
    conn = get_db()
    cur = conn.execute("SELECT * FROM agent_memory WHERE id=?", (memory_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def search_memory(
    *,
    codes: list[str] | None = None,
    keyword: str | None = None,
    memory_level: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> list[dict]:
    conn = get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if codes:
        or_parts = []
        for c in codes:
            or_parts.append("codes LIKE ?")
            params.append(f"%{c}%")
        clauses.append(f"({' OR '.join(or_parts)})")
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    if memory_level:
        clauses.append("memory_level=?")
        params.append(memory_level)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM agent_memory {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    )
    return [dict(r) for r in cur.fetchall()]


def get_recent_memories(
    *,
    memory_type: str | None = None,
    memory_level: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    conn = get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if memory_type:
        clauses.append("memory_type=?")
        params.append(memory_type)
    if memory_level:
        clauses.append("memory_level=?")
        params.append(memory_level)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM agent_memory {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    )
    return [dict(r) for r in cur.fetchall()]


def search_memory_by_keywords(keywords: list[str], limit: int = 5) -> list[dict]:
    conn = get_db()
    if not keywords:
        return []
    clauses = ["content LIKE ?" for _ in keywords]
    params = [f"%{kw}%" for kw in keywords]
    cur = conn.execute(
        f"SELECT * FROM agent_memory WHERE ({' OR '.join(clauses)}) ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    )
    return [dict(r) for r in cur.fetchall()]


def search_memory_fts(query: str, limit: int = 10) -> list[dict]:
    """FTS5 全文检索记忆。"""
    conn = get_db()
    try:
        cur = conn.execute(
            """SELECT m.*, bm25(agent_memory_fts) AS rank
               FROM agent_memory_fts fts
               JOIN agent_memory m ON m.id = fts.rowid
               WHERE agent_memory_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _memory_codes(row: dict) -> set[str]:
    raw = str(row.get("codes", "") or "")
    return {part.strip() for part in raw.split(",") if len(part.strip()) == 6 and part.strip().isdigit()}


def _scope_memory_results(candidates: dict[int, dict], codes: list[str] | None) -> dict[int, dict]:
    current_codes = set(codes or [])
    scoped: dict[int, dict] = {}
    for mid, row in candidates.items():
        mem_codes = _memory_codes(row)
        if not mem_codes or mem_codes & current_codes:
            scoped[mid] = row
    return scoped


def _memory_decay(row: dict, age_days: float, fallback_half_life_days: float) -> float:
    import math

    if row.get("memory_type") in _MEMORY_NO_DECAY_TYPES:
        return 1.0
    half_life = _MEMORY_DECAY_HALF_LIFE_DAYS.get(str(row.get("memory_type") or ""), fallback_half_life_days)
    return math.pow(2, -age_days / max(half_life, 1.0))


def _memory_age_days(row: dict) -> float | None:
    created = row.get("created_at", "")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created))
    except (ValueError, TypeError):
        return None
    return max((datetime.utcnow() - dt).total_seconds() / 86400, 0)


def search_memory_hybrid(
    *,
    query_text: str,
    codes: list[str] | None = None,
    keywords: list[str] | None = None,
    limit: int = 8,
    decay_half_life_days: float = 14.0,
    strict_code_scope: bool = False,
) -> list[dict]:
    """Hybrid search: FTS5 全文 + 代码匹配 + 关键词 LIKE + 时间衰减加权。

    返回按综合得分排序的记忆列表，每条带 _score 字段。
    """
    candidates: dict[int, dict] = {}

    def _merge(items: list[dict], source_weight: float) -> None:
        for m in items:
            mid = m["id"]
            if mid not in candidates:
                m["_score"] = source_weight
                candidates[mid] = m
            else:
                candidates[mid]["_score"] = max(candidates[mid].get("_score", 0), source_weight)

    # 1. FTS5 全文检索（最高权重）
    if query_text and len(query_text.strip()) >= 2:
        fts_results = search_memory_fts(query_text, limit=limit * 2)
        _merge(fts_results, _MEMORY_RECALL_WEIGHTS["fts"])

    # 2. 股票代码精确匹配
    if codes:
        code_results = search_memory(codes=codes, limit=limit * 2)
        _merge(code_results, _MEMORY_RECALL_WEIGHTS["code"])

    # 3. 关键词 LIKE 检索
    if keywords:
        kw_results = search_memory_by_keywords(keywords, limit=limit * 2)
        _merge(kw_results, _MEMORY_RECALL_WEIGHTS["keyword"])

    if strict_code_scope:
        candidates = _scope_memory_results(candidates, codes)

    for m in candidates.values():
        age_days = _memory_age_days(m)
        decay = 0.5 if age_days is None else _memory_decay(m, age_days, decay_half_life_days)
        m["_score"] = m.get("_score", 0.5) * decay

    # 按得分排序
    ranked = sorted(candidates.values(), key=lambda x: x.get("_score", 0), reverse=True)
    return ranked[:limit]


def _prune_agent_memory(conn: sqlite3.Connection, *, fallback_keep_days: int) -> int:
    deleted = 0
    for memory_type, keep_days in _MEMORY_RETENTION_DAYS.items():
        cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat()
        cur = conn.execute(
            "DELETE FROM agent_memory WHERE memory_type=? AND created_at < ?",
            (memory_type, cutoff),
        )
        deleted += cur.rowcount
    cutoff = (datetime.utcnow() - timedelta(days=fallback_keep_days)).isoformat()
    cur = conn.execute(
        "DELETE FROM agent_memory WHERE created_at < ? AND memory_type NOT IN (?, ?)",
        (cutoff, "preference", "persona"),
    )
    return deleted + cur.rowcount


prune_agent_memory_for_connection = _prune_agent_memory


def prune_memories(keep_days: int = 90) -> int:
    conn = get_db()
    with conn:
        return _prune_agent_memory(conn, fallback_keep_days=keep_days)


# ---------------------------------------------------------------------------
# Sync metadata
# ---------------------------------------------------------------------------


def update_sync_meta(table_name: str, row_count: int) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO sync_meta
               (table_name, last_synced_at, row_count) VALUES (?, datetime('now'), ?)""",
            (table_name, row_count),
        )


def get_sync_meta(table_name: str) -> dict | None:
    conn = get_db()
    cur = conn.execute("SELECT * FROM sync_meta WHERE table_name=?", (table_name,))
    row = cur.fetchone()
    return dict(row) if row else None


def needs_sync(table_name: str, max_age_hours: int = 6) -> bool:
    meta = get_sync_meta(table_name)
    if not meta:
        return True
    try:
        last = datetime.fromisoformat(meta["last_synced_at"])
        return datetime.utcnow() - last > timedelta(hours=max_age_hours)
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Chat log — 对话记录
# ---------------------------------------------------------------------------


def save_chat_log(
    session_id: str,
    role: str,
    content: str,
    *,
    model: str = "",
    provider: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    elapsed_s: float = 0,
    error: str = "",
    tool_calls_json: str = "",
    metadata_json: str = "",
) -> int:
    conn = get_db()
    with conn:
        cur = conn.execute(
            """INSERT INTO chat_log
               (session_id, role, content, model, provider,
                tokens_in, tokens_out, elapsed_s, error, tool_calls, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                model,
                provider,
                tokens_in,
                tokens_out,
                elapsed_s,
                error,
                tool_calls_json,
                metadata_json,
            ),
        )
        return cur.lastrowid or 0


def load_chat_logs(*, session_id: str | None = None, limit: int = 200) -> list[dict]:
    conn = get_db()
    if session_id:
        cur = conn.execute(
            "SELECT * FROM chat_log WHERE session_id=? ORDER BY created_at ASC LIMIT ?",
            (session_id, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM chat_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def save_background_task_result(
    task_id: str,
    tool_name: str,
    result: Any,
    *,
    session_id: str = "",
    status: str = "completed",
) -> int:
    """Persist a completed CLI background task result for dashboard history."""
    result_json = json.dumps(result, ensure_ascii=False, default=str)
    summary = background_task_result_summary(tool_name, task_id, result, result_json)
    conn = get_db()
    with conn:
        cur = conn.execute(
            """INSERT OR REPLACE INTO background_task_result
               (task_id, session_id, tool_name, status, result_json, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (task_id, session_id, tool_name, status, result_json, summary),
        )
        return cur.lastrowid or 0


def background_task_result_summary(
    tool_name: str,
    task_id: str,
    result: Any,
    result_json: str | None = None,
) -> str:
    try:
        from utils.tool_result_preview import serialize_tool_result, tool_result_preview

        content = result_json if result_json is not None else serialize_tool_result(result)
        if len(content) <= 3000:
            return content
        return tool_result_preview(tool_name, result, content)
    except Exception:
        raw = result_json if result_json is not None else json.dumps(result, ensure_ascii=False, default=str)
        return raw[:2000] + ("..." if len(raw) > 2000 else "")


def load_background_task_results(*, limit: int = 100) -> list[dict]:
    conn = get_db()
    cur = conn.execute(
        """SELECT id, task_id, session_id, tool_name, status, summary, created_at
           FROM background_task_result
           ORDER BY created_at DESC
           LIMIT ?""",
        (min(max(limit, 1), 500),),
    )
    return [dict(r) for r in cur.fetchall()]


def load_background_task_result(task_id: str) -> dict | None:
    conn = get_db()
    cur = conn.execute(
        "SELECT * FROM background_task_result WHERE task_id=?",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["result"] = json.loads(data.get("result_json") or "{}")
    except json.JSONDecodeError:
        data["result"] = data.get("result_json") or ""
    return data


def get_session_preview(session_id: str) -> str:
    """取会话首条用户消息作为摘要预览。"""
    conn = get_db()
    cur = conn.execute(
        "SELECT content FROM chat_log WHERE session_id=? AND role='user' ORDER BY created_at ASC LIMIT 1",
        (session_id,),
    )
    row = cur.fetchone()
    if row:
        t = (row["content"] or "").strip().replace("\n", " ")
        return t[:60] + ("…" if len(t) > 60 else "")
    return "(空会话)"


# ---------------------------------------------------------------------------
# Tail-buy history
# ---------------------------------------------------------------------------


_TAIL_BUY_COLUMNS = [
    "code",
    "name",
    "run_date",
    "signal_date",
    "signal_type",
    "status",
    "final_decision",
    "rule_decision",
    "rule_score",
    "priority_score",
    "rule_reasons",
    "llm_decision",
    "llm_reason",
    "llm_confidence",
    "llm_model_used",
    "initial_price",
    "current_price",
    "change_pct",
    "price_updated_at",
    "last_close",
    "vwap",
    "dist_vwap_pct",
    "close_pos",
    "day_ret_pct",
    "last30_ret_pct",
    "last15_ret_pct",
    "tail30_volume_share",
    "drop_from_high_pct",
    "fetch_error",
    "features_json",
]


def save_tail_buy_results(rows: list[dict]) -> int:
    return _bulk_upsert(
        get_db(),
        "tail_buy_history",
        _TAIL_BUY_COLUMNS,
        rows,
        _tail_buy_insert_values,
        timestamp_column="created_at",
    )


def _tail_buy_insert_values(r: dict) -> tuple[Any, ...]:
    return (
        str(r.get("code", "")).strip(),
        str(r.get("name", "")).strip(),
        str(r.get("run_date", "")).strip(),
        str(r.get("signal_date", "")).strip(),
        str(r.get("signal_type", "")).strip(),
        str(r.get("status", "")).strip(),
        str(r.get("final_decision", "")).strip(),
        str(r.get("rule_decision", "")).strip(),
        float(r.get("rule_score", 0) or 0),
        float(r.get("priority_score", 0) or 0),
        str(r.get("rule_reasons", "")).strip(),
        str(r.get("llm_decision", "")).strip(),
        str(r.get("llm_reason", "")).strip(),
        r.get("llm_confidence"),
        str(r.get("llm_model_used", "")).strip(),
        float(r.get("initial_price", 0) or 0),
        float(r.get("current_price", 0) or 0),
        float(r.get("change_pct", 0) or 0),
        str(r.get("price_updated_at", "")).strip(),
        float(r.get("last_close", 0) or 0),
        float(r.get("vwap", 0) or 0),
        float(r.get("dist_vwap_pct", 0) or 0),
        float(r.get("close_pos", 0) or 0),
        float(r.get("day_ret_pct", 0) or 0),
        float(r.get("last30_ret_pct", 0) or 0),
        float(r.get("last15_ret_pct", 0) or 0),
        float(r.get("tail30_volume_share", 0) or 0),
        float(r.get("drop_from_high_pct", 0) or 0),
        str(r.get("fetch_error", "")).strip(),
        str(r.get("features_json", "")).strip(),
    )


def load_tail_buy_history(
    *,
    run_date: str = "",
    decision: str = "",
    limit: int = 50,
) -> list[dict]:
    conn = get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if run_date:
        clauses.append("run_date = ?")
        params.append(run_date.strip())
    if decision:
        clauses.append("final_decision = ?")
        params.append(decision.strip().upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM tail_buy_history {where} ORDER BY run_date DESC, priority_score DESC LIMIT ?",
        params + [min(max(limit, 1), 200)],
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------


def delete_chat_session(session_id: str) -> int:
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM chat_log WHERE session_id=?",
            (session_id,),
        )
    return cur.rowcount


def list_chat_sessions(limit: int = 50) -> list[dict]:
    """返回最近的会话列表，每个会话的首条用户消息作为摘要。"""
    conn = get_db()
    cur = conn.execute(
        """SELECT session_id,
                  MIN(created_at) AS started_at,
                  MAX(created_at) AS ended_at,
                  COUNT(*) AS msg_count,
                  SUM(tokens_in) AS total_tokens_in,
                  SUM(tokens_out) AS total_tokens_out,
                  MAX(CASE WHEN error != '' THEN error ELSE NULL END) AS last_error,
                  MAX(CASE WHEN role='assistant' THEN model ELSE NULL END) AS model,
                  (SELECT content FROM chat_log c2 WHERE c2.session_id=chat_log.session_id AND c2.role='user' ORDER BY c2.created_at ASC LIMIT 1) AS first_user_msg,
                  SUM(elapsed_s) AS total_elapsed_s
           FROM chat_log
           GROUP BY session_id
           ORDER BY MAX(created_at) DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_old_records(days: int = 30) -> dict[str, int]:
    """删除 N 天前的 chat_log / background_task_result / agent_memory 记录。"""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    deleted: dict[str, int] = {}
    with conn:
        for table in ("chat_log", "background_task_result", "workflow_run"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE created_at < ?",
                (cutoff,),
            )
            deleted[table] = cur.rowcount
        cur = conn.execute(
            """DELETE FROM workflow_event
               WHERE run_id NOT IN (SELECT run_id FROM workflow_run)""",
        )
        deleted["workflow_event"] = cur.rowcount
        deleted["agent_memory"] = _prune_agent_memory(conn, fallback_keep_days=days)
    return deleted
