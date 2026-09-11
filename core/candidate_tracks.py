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


#: 车道 slug 的字面形状:ASCII 字母数字加分隔符。所有 13 个合法取值(sos/evr/lps/
#: spring/compression/trend_pullback/mainline/…)都落在里面,而中文买点理由
#: (「主线回踩MA5 + 主线平台再突破」)落不进来。
_LANE_SLUG_SHAPE = re.compile(r"[A-Za-z0-9 _-]+")


#: ``selection_source`` 在市场闸门关闭时被追加的后缀（``_tracking_source``）。它标的是
#: 「这一行写入时市场闸门是关的」,属于来源/状态维度,不是车道身份的一部分。
MARKET_BLOCK_SUFFIX = ":market_blocked"


def strip_lane_status_suffix(raw: Any) -> str:
    """把车道取值里的市场闸门后缀摘掉。

    ``recommendation_payload`` 在 candidate_lane 缺失时会退到 ``selection_source``,
    而那个字段此时已经带了 ``:market_blocked``,于是同一条车道按市场状态被劈成两个
    标签:实测 ``signal_confirmed`` 开市侧 14 行、拦截侧 94 行,跨 13 个交易日。
    任何按车道汇总的归因都会把它们当两条车道,而占多数的那半还是带后缀的。
    """
    text = str(raw or "").strip()
    if text.endswith(MARKET_BLOCK_SUFFIX):
        return text[: -len(MARKET_BLOCK_SUFFIX)].strip()
    return text


def lane_slug_or(raw: Any, fallback: str) -> str:
    """把 ``raw`` 当车道 slug 用,形状不对就退回 ``fallback``。

    ``entry_type`` 这一列的契约是车道 slug,可主线链路曾把 ``_timing_result`` 的
    中文买点理由原样塞进来——同一列两种语义,和 ``candidate_status`` 当年一模一样。
    危害在下游的 group by:``_signal_context_key`` 和治理器的 ``_context_scope``
    都拿它当分组键,29 个散文键里只有 4 个够 ``MIN_CONTEXT_SAMPLES``,63 行主线候选
    有 36 行永远进不了治理器,剩下的还被切成 5~8 样本的碎片当「样本不足」处理。

    理由文本本身有 ``timing``/``candidate_timing`` 承载,不在这里丢。
    """
    text = str(raw or "").strip()
    if not text or not _LANE_SLUG_SHAPE.fullmatch(text):
        return fallback
    return normalize_candidate_entry_key(text) or fallback


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
