"""Keep machine-readable numbers out of the LLM rewrite path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

NUMERIC_KEYS = frozenset(
    {
        "close",
        "score",
        "watch_score",
        "win_rate_pct",
        "avg_return_pct",
        "sample_count",
        "input_count",
        "selected_count",
        "veto_count",
    }
)


def freeze_evidence(source: dict[str, Any]) -> dict[str, Any]:
    numbers = {key: source[key] for key in NUMERIC_KEYS if key in source}
    return {
        "schema": "wyckoff_evidence_v1",
        "numbers": numbers,
        "codes": list(source.get("codes") or []),
        "veto_lines": list(source.get("veto_lines") or []),
    }


def merge_llm_prose(evidence: dict[str, Any], prose: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    frozen = dict(evidence)
    frozen_numbers = dict(frozen.get("numbers") or {})
    overlay = dict(extra or {})
    overlay_numbers = overlay.pop("numbers", None)
    if overlay_numbers:
        raise ValueError("LLM overlay cannot replace evidence numbers")
    frozen["prose"] = str(prose or "")
    frozen.update(overlay)
    frozen["numbers"] = frozen_numbers
    return frozen


def numbers_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return dict(before.get("numbers") or {}) == dict(after.get("numbers") or {})


def write_evidence_snapshot(evidence: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
