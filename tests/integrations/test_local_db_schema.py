from __future__ import annotations

import sqlite3


def _reset_local_db(local_db) -> None:
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


def test_init_db_migrates_candidate_columns_before_lane_indexes(tmp_path, monkeypatch):
    from integrations import local_db

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO schema_version(version) VALUES(12);
            CREATE TABLE recommendation_tracking (
                code TEXT NOT NULL,
                name TEXT DEFAULT '',
                recommend_date INTEGER NOT NULL,
                UNIQUE(code, recommend_date)
            );
            CREATE TABLE signal_pending (
                code TEXT NOT NULL,
                name TEXT DEFAULT '',
                signal_type TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                UNIQUE(code, signal_type, signal_date)
            );
            """
        )

    _reset_local_db(local_db)
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", db_path)
    try:
        local_db.init_db()
        conn = local_db.get_db()
        rec_columns = {row["name"] for row in conn.execute("PRAGMA table_info(recommendation_tracking)")}
        sig_columns = {row["name"] for row in conn.execute("PRAGMA table_info(signal_pending)")}
        rec_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(recommendation_tracking)")}
        sig_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(signal_pending)")}

        assert "candidate_lane" in rec_columns
        assert "candidate_lane" in sig_columns
        assert "idx_rec_lane" in rec_indexes
        assert "idx_sig_lane" in sig_indexes
    finally:
        _reset_local_db(local_db)
