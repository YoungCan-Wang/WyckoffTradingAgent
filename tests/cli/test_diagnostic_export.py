from __future__ import annotations

import json
import zipfile
from pathlib import Path

from cli.diagnostic_export import DiagnosticExportError, export_diagnostic_package


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


def test_export_diagnostic_package_zip_includes_session_evidence(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("WYCKOFF_HOME", str(home))
    local_db = _init_tmp_db(monkeypatch, tmp_path)
    try:
        scratchpad = home / "scratchpad" / "run.jsonl"
        scratchpad.parent.mkdir(parents=True)
        scratchpad.write_text('{"type":"init","content":"看看 000001"}\n', encoding="utf-8")

        tool_result = home / "tool-results" / "result.json"
        tool_result.parent.mkdir(parents=True)
        tool_result.write_text('{"rows":[{"code":"000001"}]}', encoding="utf-8")
        other_result = home / "tool-results" / "other.json"
        other_result.write_text("{}", encoding="utf-8")
        (home / "tool-results" / "index.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"node_id": "T_keep", "result_ref": str(tool_result)}, ensure_ascii=False),
                    json.dumps({"node_id": "T_skip", "result_ref": str(other_result)}, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        local_db.save_chat_log("session_x", "user", "看看 000001")
        local_db.save_chat_log(
            "session_x",
            "assistant",
            f"完成\nresult_ref: {tool_result}",
            model="test-model",
            provider="test-provider",
            tokens_in=12,
            tokens_out=8,
            tool_calls_json='[{"name":"analyze_stock"}]',
            metadata_json=json.dumps(
                {
                    "scratchpad_path": str(scratchpad),
                    "messages": [{"role": "user", "content": "看看 000001"}],
                    "api_key": "secret-value",
                },
                ensure_ascii=False,
            ),
        )

        result = export_diagnostic_package(session_id="session_x", output=tmp_path / "diag.zip")

        assert result.session_id == "session_x"
        assert result.message_count == 2
        assert result.scratchpad_count == 1
        assert result.tool_result_count == 1
        with zipfile.ZipFile(result.path) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "chat_log.json" in names
            assert "transcript.md" in names
            assert "events.jsonl" in names
            assert "scratchpads/run.jsonl" in names
            assert "tool-results/result.json" in names
            assert "tool-results/other.json" not in names
            chat_log = json.loads(zf.read("chat_log.json").decode("utf-8"))
            assert chat_log[1]["metadata"]["api_key"] == "***REDACTED***"
            index_lines = zf.read("tool-results/index.jsonl").decode("utf-8").splitlines()
            assert len(index_lines) == 1
            assert json.loads(index_lines[0])["node_id"] == "T_keep"
            transcript = zf.read("transcript.md").decode("utf-8")
            assert "看看 000001" in transcript
            events = [json.loads(line) for line in zf.read("events.jsonl").decode("utf-8").splitlines()]
            assert events[0]["schema"] == "wyckoff.agent_event.v1"
            assert events[0]["type"] == "user_message"
    finally:
        _close_tmp_db(local_db)


def test_export_diagnostic_package_errors_when_session_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_HOME", str(tmp_path / "home"))
    local_db = _init_tmp_db(monkeypatch, tmp_path)
    try:
        try:
            export_diagnostic_package(session_id="missing", output=tmp_path / "diag.zip")
        except DiagnosticExportError as exc:
            assert "未找到会话" in str(exc)
        else:
            raise AssertionError("expected DiagnosticExportError")
    finally:
        _close_tmp_db(local_db)
