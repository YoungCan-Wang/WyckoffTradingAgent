"""Observation-only recall lanes derived from point-in-time funnel decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.funnel_taxonomy import (
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_MISS,
)

SHADOW_POLICY_VERSION = "review_shadow_v1"
NEAR_L2_MAX_GAP_PCT = 10.0


@dataclass(frozen=True)
class ReviewShadowSignal:
    lane: str
    score: float
    reason: str
    policy_version: str = SHADOW_POLICY_VERSION


def shadow_signal_from_decision(
    row: dict[str, Any],
    *,
    near_l2_max_gap_pct: float = NEAR_L2_MAX_GAP_PCT,
) -> ReviewShadowSignal | None:
    """Classify one as-of decision row without looking at later prices."""
    stage = str(row.get("stage") or "")
    if stage == REVIEW_STAGE_RISK_BLOCK or str(row.get("risk_signal") or ""):
        return None
    if stage == REVIEW_STAGE_STRENGTH_MISS:
        return _near_l2_signal(str(row.get("reason") or ""), near_l2_max_gap_pct)
    if stage == REVIEW_STAGE_THEME_MISS and bool(row.get("l2_eligible")):
        channel = str(row.get("l2_channel") or "结构通道")
        return ReviewShadowSignal("rotation_setup", 72.0, f"已通过{channel}，等待板块轮动确认")
    if stage == REVIEW_STAGE_TRIGGER_MISS and bool(row.get("l3_eligible")):
        channel = str(row.get("l2_channel") or "结构通道")
        return ReviewShadowSignal("pre_breakout", 78.0, f"已通过L1-L3及{channel}，等待爆发前夜确认")
    return None


def attach_shadow_signal(row: dict[str, Any], *, near_l2_max_gap_pct: float = NEAR_L2_MAX_GAP_PCT) -> dict[str, Any]:
    signal = shadow_signal_from_decision(row, near_l2_max_gap_pct=near_l2_max_gap_pct)
    if signal is None:
        return row
    return {
        **row,
        "shadow_lane": signal.lane,
        "shadow_score": signal.score,
        "shadow_reason": signal.reason,
        "shadow_policy_version": signal.policy_version,
    }


def shadow_lane_label(lane: str) -> str:
    return {
        "near_l2": "接近结构通道",
        "rotation_setup": "轮动待确认",
        "pre_breakout": "爆发前夜",
    }.get(str(lane or ""), str(lane or ""))


def _near_l2_signal(reason: str, maximum: float) -> ReviewShadowSignal | None:
    match = re.search(r"缺口\s*([0-9]+(?:\.[0-9]+)?)%", reason)
    if not match:
        return None
    gap = float(match.group(1))
    if gap <= 0 or gap > max(float(maximum), 0.0):
        return None
    score = max(0.0, 70.0 - gap * 2.0)
    return ReviewShadowSignal("near_l2", score, f"距离最接近L2通道仅{gap:.1f}%")
