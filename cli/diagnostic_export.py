"""Export inspectable diagnostic packages for CLI agent sessions."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cli.scratchpad import is_sensitive_key, wyckoff_home

_TEXT_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bBearer\s+eyJ[A-Za-z0-9_.-]+"),
    re.compile(r"\b(?:ANTHROPIC|OPENAI|OPENROUTER|GEMINI|TUSHARE|SUPABASE)_[A-Z0-9_]*KEY\s*=\s*\S+"),
)
_RESULT_REF_RE = re.compile(r"result_ref:\s*([^\s`]+)")


class DiagnosticExportError(RuntimeError):
    """Raised when a diagnostic package cannot be exported."""


@dataclass(frozen=True)
class DiagnosticExportResult:
    path: Path
    session_id: str
    message_count: int
    scratchpad_count: int
    tool_result_count: int


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in _TEXT_SECRET_PATTERNS:
        redacted = pattern.sub("***REDACTED***", redacted)
    return redacted


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            cleaned[key_text] = "***REDACTED***" if is_sensitive_key(key_text) else _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _loads_json_maybe(text: str) -> Any:
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _redact_text(text)


def _default_output_path(session_id: str, *, output_format: str) -> Path:
    out_dir = wyckoff_home() / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    suffix = "zip" if output_format == "zip" else "json"
    return out_dir / f"wyckoff-diagnostic-{session_id[:8]}-{stamp}.{suffix}"


def _latest_session_id() -> str:
    from integrations.local_db import list_chat_sessions

    sessions = list_chat_sessions(limit=1)
    if not sessions:
        raise DiagnosticExportError("暂无可导出的对话会话")
    return str(sessions[0]["session_id"])


def _normalize_log(row: dict[str, Any]) -> dict[str, Any]:
    entry = dict(row)
    entry["content"] = _redact_text(str(entry.get("content", "") or ""))
    entry["tool_calls"] = _scrub(_loads_json_maybe(str(entry.get("tool_calls", "") or "")))
    entry["metadata"] = _scrub(_loads_json_maybe(str(entry.get("metadata", "") or "")))
    return entry


def _session_manifest(session_id: str, logs: list[dict[str, Any]]) -> dict[str, Any]:
    first_user = next((row.get("content", "") for row in logs if row.get("role") == "user"), "")
    models = sorted({str(row.get("model", "") or "") for row in logs if row.get("model")})
    providers = sorted({str(row.get("provider", "") or "") for row in logs if row.get("provider")})
    return {
        "schema": "wyckoff.diagnostic.v1",
        "session_id": session_id,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "message_count": len(logs),
        "first_user_message": str(first_user)[:500],
        "models": models,
        "providers": providers,
        "total_tokens_in": sum(int(row.get("tokens_in") or 0) for row in logs),
        "total_tokens_out": sum(int(row.get("tokens_out") or 0) for row in logs),
        "total_elapsed_s": round(sum(float(row.get("elapsed_s") or 0) for row in logs), 3),
    }


def _transcript_markdown(session_id: str, logs: list[dict[str, Any]]) -> str:
    lines = ["# Wyckoff Diagnostic Transcript", "", f"- session_id: `{session_id}`", ""]
    for row in logs:
        role = str(row.get("role", "") or "unknown")
        created_at = str(row.get("created_at", "") or "")
        lines.append(f"## {role} {created_at}".strip())
        model = str(row.get("model", "") or "")
        tokens = f"{int(row.get('tokens_in') or 0)}/{int(row.get('tokens_out') or 0)}"
        if model or tokens != "0/0":
            lines.append(f"- model: `{model or '-'}`")
            lines.append(f"- tokens: `{tokens}`")
        tool_calls = row.get("tool_calls")
        if tool_calls:
            lines.append(f"- tool_calls: `{json.dumps(tool_calls, ensure_ascii=False)[:1000]}`")
        content = str(row.get("content", "") or "")
        if content:
            lines.extend(["", content])
        error = str(row.get("error", "") or "")
        if error:
            lines.extend(["", f"ERROR: {error}"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _safe_under_home(path: Path, home: Path) -> bool:
    try:
        path.resolve().relative_to(home.resolve())
        return True
    except ValueError:
        return False


def _collect_referenced_files(logs: list[dict[str, Any]]) -> tuple[list[Path], list[Path]]:
    home = wyckoff_home()
    scratchpads: dict[str, Path] = {}
    tool_results: dict[str, Path] = {}

    def add_path(raw: str, bucket: dict[str, Path]) -> None:
        if not raw:
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return
        if not path.exists() or not path.is_file():
            return
        if not _safe_under_home(path, home):
            return
        bucket[str(path.resolve())] = path

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            scratchpad_path = value.get("scratchpad_path")
            if isinstance(scratchpad_path, str):
                add_path(scratchpad_path, scratchpads)
            for item in value.values():
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, str):
            for match in _RESULT_REF_RE.finditer(value):
                add_path(match.group(1), tool_results)

    for row in logs:
        walk(row)

    return list(scratchpads.values()), list(tool_results.values())


def _build_payload(session_id: str, logs: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_logs = [_normalize_log(row) for row in logs]
    scratchpads, tool_results = _collect_referenced_files(normalized_logs)
    manifest = _session_manifest(session_id, normalized_logs)
    manifest["scratchpad_count"] = len(scratchpads)
    manifest["tool_result_count"] = len(tool_results)
    return {
        "manifest": manifest,
        "chat_log": normalized_logs,
        "transcript": _transcript_markdown(session_id, normalized_logs),
        "scratchpads": scratchpads,
        "tool_results": tool_results,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    from cli.event_stream import load_scratchpad_events

    data = {
        "manifest": payload["manifest"],
        "chat_log": payload["chat_log"],
        "transcript": payload["transcript"],
        "events": [event for file_path in payload["scratchpads"] for event in load_scratchpad_events(file_path)],
        "referenced_files": {
            "scratchpads": [str(path) for path in payload["scratchpads"]],
            "tool_results": [str(path) for path in payload["tool_results"]],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _write_zip(path: Path, payload: dict[str, Any]) -> None:
    from cli.event_stream import scratchpad_events_jsonl

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(payload["manifest"], ensure_ascii=False, indent=2))
        zf.writestr("chat_log.json", json.dumps(payload["chat_log"], ensure_ascii=False, indent=2, default=str))
        zf.writestr("transcript.md", payload["transcript"])
        events_text = scratchpad_events_jsonl(payload["scratchpads"])
        if events_text:
            zf.writestr("events.jsonl", events_text)
        for file_path in payload["scratchpads"]:
            zf.write(file_path, f"scratchpads/{file_path.name}")
        for file_path in payload["tool_results"]:
            zf.write(file_path, f"tool-results/{file_path.name}")
        index_text = _filtered_tool_result_index(payload["tool_results"])
        if index_text:
            zf.writestr("tool-results/index.jsonl", index_text)


def _filtered_tool_result_index(tool_results: list[Path]) -> str:
    index_path = wyckoff_home() / "tool-results" / "index.jsonl"
    if not tool_results or not index_path.exists() or not index_path.is_file():
        return ""
    refs = {str(path.resolve()) for path in tool_results}
    lines: list[str] = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        result_ref = str(entry.get("result_ref", "") or "")
        if result_ref and str(Path(result_ref).expanduser().resolve()) in refs:
            lines.append(json.dumps(_scrub(entry), ensure_ascii=False, default=str))
    return "\n".join(lines) + ("\n" if lines else "")


def export_diagnostic_package(
    *,
    session_id: str = "",
    output: Path | None = None,
    output_format: str = "zip",
) -> DiagnosticExportResult:
    """Export a session diagnostic package.

    The package includes scrubbed chat logs, a Markdown transcript, and local
    scratchpad/tool-result files referenced by the session metadata.
    """

    if output_format not in {"zip", "json"}:
        raise DiagnosticExportError("output_format 仅支持 zip/json")

    from integrations.local_db import init_db, load_chat_logs

    init_db()
    resolved_session_id = session_id.strip() or _latest_session_id()
    logs = load_chat_logs(session_id=resolved_session_id, limit=1000)
    if not logs:
        raise DiagnosticExportError(f"未找到会话: {resolved_session_id}")

    path = output or _default_output_path(resolved_session_id, output_format=output_format)
    payload = _build_payload(resolved_session_id, logs)
    if output_format == "zip":
        if path.suffix.lower() != ".zip":
            path = path.with_suffix(".zip")
        _write_zip(path, payload)
    else:
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        _write_json(path, payload)

    manifest = payload["manifest"]
    return DiagnosticExportResult(
        path=path,
        session_id=resolved_session_id,
        message_count=int(manifest["message_count"]),
        scratchpad_count=int(manifest["scratchpad_count"]),
        tool_result_count=int(manifest["tool_result_count"]),
    )
