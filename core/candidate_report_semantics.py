"""Stable report labels derived from persisted candidate metadata."""

from __future__ import annotations

import json
import math
from typing import Any

_PHASE_LABELS = {
    "主线买点候选": "主升候选",
    "强主线分歧": "分歧机会",
    "事件主题修复候选": "事件修复",
    "主线观察": "观察蓄势",
    "过热不追": "加速过热",
}


def candidate_theme(candidate_reasons: Any) -> str:
    payload = candidate_reason_payload(candidate_reasons)
    return str(payload.get("theme") or "").strip()


def candidate_phase(candidate_status: Any) -> str:
    status = str(candidate_status or "").strip()
    if status.lower() == "nan":
        return ""
    return _PHASE_LABELS.get(status, status)


def candidate_role(stock_role_score: Any, candidate_lane: Any = "") -> str:
    score = optional_candidate_score(stock_role_score)
    if score is None:
        return "主线候选" if str(candidate_lane or "").strip() == "mainline" else ""
    if score >= 0.75:
        return "主线核心"
    if score >= 0.60:
        return "强势成员"
    return "跟随观察"


def candidate_semantic_parts(
    *,
    candidate_reasons: Any,
    candidate_status: Any,
    stock_role_score: Any,
    candidate_lane: Any = "",
    explicit_theme: Any = "",
    explicit_phase: Any = "",
    explicit_role: Any = "",
) -> list[str]:
    theme = _text(explicit_theme) or candidate_theme(candidate_reasons)
    phase = _text(explicit_phase) or candidate_phase(candidate_status)
    role = _text(explicit_role) or candidate_role(stock_role_score, candidate_lane)
    return [text for text in (theme, phase, role) if text]


def candidate_reason_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def optional_candidate_score(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _text(raw: Any) -> str:
    text = str(raw or "").strip()
    return "" if text.lower() == "nan" else text
