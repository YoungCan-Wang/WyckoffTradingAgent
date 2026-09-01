"""Candidate track normalization shared by selection paths."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from core.candidate_policy import candidate_score_value

#: 正式 L4 触发通道（``_formal_candidate_entries`` 产出的六条）。判「这一行是不是
#: 正式 L4 候选」要按 ``candidate_lane`` 落在这个集合里，不要去比
#: ``candidate_status == "formal_l4"``——那个字段是语义状态位，历史上被生产者标签
#: 占用过，且在 stage 已知时会被 ``Accum_B``/``Accum_C`` 顶掉。
FORMAL_L4_LANES = frozenset({"sos", "evr", "lps", "spring", "compression", "trend_pullback"})

#: 生产者标签：候选条目 ``state`` 字段用它区分产出通道（正式触发 / alpha / Lane /
#: Mainline），是流水线内部用的来源标记，不是给人看的候选状态。
CANDIDATE_PRODUCER_TAGS = frozenset({"formal_l4", "alpha", "Lane", "Mainline"})

#: Wyckoff 阶段名（``detect_accum_stage`` + ``detect_markup_stage`` 的全部取值）。
#: 它有自己的 ``stage``/``stage_tag`` 列，不该挤进 candidate_status。
WYCKOFF_STAGE_NAMES = frozenset({"Accum_A", "Accum_B", "Accum_C", "Markup"})

ACCUM_TRACK_KEYS = {
    "accum",
    "accumulation",
    "accumulation_ready",
    "compression",
    "lps",
    "spring",
}

TREND_TRACK_KEYS = {
    "breakout",
    "evr",
    "future_leader",
    "main_force_entry",
    "mainline",
    "sos",
    "trend",
    "trend_breakout",
    "trend_lane_pullback",
    "trend_pullback",
}

CANDIDATE_ENTRY_PRIORITY = {
    "launchpad": 0,
    "tight_base": 1,
    "early_breakout": 2,
    "main_force_entry": 3,
    "mainline": 4,
    "trend_lane_pullback": 5,
    "trend_breakout": 6,
    "sector_strength": 7,
    "volatile_pullback": 8,
    "accumulation_ready": 9,
    "spring": 10,
    "lps": 11,
    "compression": 12,
    "trend_pullback": 13,
    "wyckoff_structure": 14,
    "sos": 15,
    "evr": 16,
}
UNKNOWN_CANDIDATE_ENTRY_PRIORITY = max(CANDIDATE_ENTRY_PRIORITY.values()) + 1


def normalize_candidate_track(raw: Any, *, default: str = "Trend") -> str:
    """Return canonical Trend/Accum for candidate entry track values."""

    key = normalize_candidate_entry_key(raw)
    track = _track_for_key(key)
    if track:
        return track
    return "Accum" if default == "Accum" else "Trend"


def normalize_candidate_entry_key(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    key = re.sub(r"[\s-]+", "_", key)
    return re.sub(r"_+", "_", key).strip("_")


def candidate_entry_key(
    item: Mapping[str, Any],
    known_keys: Iterable[str] | None = None,
    *,
    fields: tuple[str, ...] = ("entry_type", "signal_key", "lane"),
) -> str:
    known = {normalize_candidate_entry_key(key) for key in known_keys or []}
    fallback = ""
    for field in fields:
        key = normalize_candidate_entry_key(item.get(field))
        if not key:
            continue
        fallback = fallback or key
        if not known or key in known:
            return key
    return fallback


def candidate_entry_track(
    item: Mapping[str, Any],
    *,
    default: str = "Trend",
    fields: tuple[str, ...] = ("track", "signal_key", "lane", "entry_type"),
) -> str:
    for field in fields:
        track = _track_for_key(normalize_candidate_entry_key(item.get(field)))
        if track:
            return track
    return "Accum" if default == "Accum" else "Trend"


def candidate_entry_sort_key(item: Mapping[str, Any]) -> tuple[int, float, str]:
    entry_type = candidate_entry_key(item, CANDIDATE_ENTRY_PRIORITY.keys())
    return (
        CANDIDATE_ENTRY_PRIORITY.get(entry_type, UNKNOWN_CANDIDATE_ENTRY_PRIORITY),
        -candidate_entry_score(item),
        str(item.get("code", "")),
    )


def candidate_entry_score(item: Mapping[str, Any]) -> float:
    return candidate_score_value(item.get("score"))


def stronger_candidate_entry(new_item: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    new_score = candidate_entry_score(new_item)
    current_score = candidate_entry_score(current)
    if new_score != current_score:
        return new_score > current_score
    return candidate_entry_sort_key(new_item) < candidate_entry_sort_key(current)


def best_candidate_entry_map(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in entries or []:
        code = str((item or {}).get("code", "")).strip()
        if not code:
            continue
        current = result.get(code)
        if current is None or stronger_candidate_entry(item, current):
            result[code] = sanitized_candidate_entry(item)
    return result


def sanitized_candidate_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(item or {})
    out["score"] = candidate_entry_score(item)
    return out


def _track_for_key(key: str) -> str:
    if key in ACCUM_TRACK_KEYS:
        return "Accum"
    if key in TREND_TRACK_KEYS:
        return "Trend"
    return ""
