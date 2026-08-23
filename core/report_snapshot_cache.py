"""Bind Step3 LLM reports to a hash of the data that produced them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from utils.env import env_bool

PROMPT_VERSION = "wyckoff_funnel_system_v3"
CACHE_VERSION = "step3_snapshot_v1"


def snapshot_cache_enabled() -> bool:
    return env_bool("STEP3_SNAPSHOT_CACHE", True)


def data_snapshot_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{CACHE_VERSION}:{digest}"


def build_step3_snapshot_payload(
    *,
    trade_date: str,
    regime: str,
    model: str,
    selected_rows: list[dict[str, Any]],
    rag_veto_lines: list[str],
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, Any]:
    return {
        "trade_date": str(trade_date or ""),
        "regime": str(regime or ""),
        "model": str(model or ""),
        "prompt_version": prompt_version,
        "rag_veto_lines": list(rag_veto_lines),
        "selected_rows": [_row_fingerprint(row) for row in selected_rows],
    }


def cache_file(snapshot_hash: str, cache_dir: Path) -> Path:
    safe = snapshot_hash.replace(":", "_")
    return Path(cache_dir) / f"{safe}.json"


def load_cached_report(snapshot_hash: str, cache_dir: Path) -> dict[str, Any] | None:
    path = cache_file(snapshot_hash, cache_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("data_snapshot_hash") != snapshot_hash:
        return None
    return payload


def store_cached_report(snapshot_hash: str, report: dict[str, Any], cache_dir: Path) -> Path:
    path = cache_file(snapshot_hash, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(report)
    body["data_snapshot_hash"] = snapshot_hash
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def default_cache_dir() -> Path:
    import os

    raw = os.getenv("STEP3_SNAPSHOT_CACHE_DIR") or os.path.join(os.getenv("LOGS_DIR", "logs"), "step3_cache")
    return Path(raw)


def selected_rows_from_df(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    return frame.to_dict(orient="records")


def _row_fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "code",
        "signal_type",
        "score",
        "watch_score",
        "close",
        "industry",
        "tag",
    )
    return {key: row.get(key) for key in keep if key in row}
