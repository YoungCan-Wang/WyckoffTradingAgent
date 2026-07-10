from __future__ import annotations

import json
from pathlib import Path

from cli.session_tools import export_session_transcript, fork_session


def _init_tmp_db(monkeypatch, tmp_path: Path):
    import integrations.local_db as local_db

    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "wyckoff.db")
    local_db.init_db()
    return local_db


def _close_tmp_db(local_db) -> None:
    if local_db._conn is not None:
        local_db._conn.close()
    local_db._conn = None


def test_export_session_transcript_markdown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path / "home"))
    local_db = _init_tmp_db(monkeypatch, tmp_path)
    try:
        local_db.save_chat_log("source", "user", "复盘 000001")
        local_db.save_chat_log(
            "source",
            "assistant",
            "已完成",
            provider="gemini",
            model="gemini-2.0-flash",
            tokens_in=10,
            tokens_out=6,
        )

        result = export_session_transcript(session_id="source", output=tmp_path / "session.md")

        assert result.message_count == 2
        text = result.path.read_text(encoding="utf-8")
        assert "复盘 000001" in text
        assert "gemini/gemini-2.0-flash" in text
    finally:
        _close_tmp_db(local_db)


def test_fork_session_copies_rows_with_fork_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path / "home"))
    local_db = _init_tmp_db(monkeypatch, tmp_path)
    try:
        local_db.save_chat_log("source", "user", "看一下持仓", metadata_json=json.dumps({"a": 1}))
        local_db.save_chat_log("source", "assistant", "好的")

        result = fork_session(session_id="source", new_session_id="branch")
        rows = local_db.load_chat_logs(session_id="branch", limit=10)

        assert result.source_session_id == "source"
        assert result.new_session_id == "branch"
        assert len(rows) == 2
        assert rows[0]["content"] == "看一下持仓"
        assert json.loads(rows[0]["metadata"])["_fork"]["source_session_id"] == "source"
    finally:
        _close_tmp_db(local_db)
