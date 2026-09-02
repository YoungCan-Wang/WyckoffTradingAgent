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

SHADOW_POLICY_VERSION = "review_shadow_v2"
NEAR_L2_MAX_GAP_PCT = 10.0

# 常数分不是分:v1 里 pre_breakout 恒为 78.0、rotation_setup 恒为 72.0,
# 2026-09-01 那份复盘的 31 只「买点未确认」因此全同分。同分的车道排不了序,
# 「取前 N 只看超额」这类效果检验就无从下手。v2 的处理:
#   - pre_breakout 用漏斗自己算过的 watch_score(layer3_score_map)当排序键,
#     它是 0.25*q20+0.20*q5+0.05*q3+0.20*dry_q+0.30*trigger_q ± 加减项,
#     取值约 [0,1],乘 100 落到 0~100 与另两条车道同量纲
#   - rotation_setup 所在层(题材共振)没有连续键,不硬造一个:score 置 None,
#     ranked=False,当标签用。下游要排序时按 ranked 过滤,而不是拿它跟别的比
_PRE_BREAKOUT_SCORE_FLOOR = 40.0


@dataclass(frozen=True)
class ReviewShadowSignal:
    lane: str
    score: float | None
    reason: str
    policy_version: str = SHADOW_POLICY_VERSION
    ranked: bool = True


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
        return ReviewShadowSignal(
            "rotation_setup", None, f"已通过{channel}，等待板块轮动确认（本层无连续排序键，仅作标签）", ranked=False
        )
    if stage == REVIEW_STAGE_TRIGGER_MISS and bool(row.get("l3_eligible")):
        return _pre_breakout_signal(row)
    return None


def _pre_breakout_signal(row: dict[str, Any]) -> ReviewShadowSignal:
    """排序键取漏斗自己算的 watch_score,不再返回常数 78.0。

    watch_score 在 rank_l3_candidates 里对 L3 全体算过一次,已含动量分位、缩量
    程度和触发强度,是这一层唯一现成的连续键。老 trace 没有这个字段,此时退回
    不可排序标签——宁可显式说明排不了序,也不要用常数假装能排。
    """
    channel = str(row.get("l2_channel") or "结构通道")
    raw = row.get("layer3_quality_score")
    try:
        watch = float(raw)
    except (TypeError, ValueError):
        return ReviewShadowSignal(
            "pre_breakout",
            None,
            f"已通过L1-L3及{channel}，等待爆发前夜确认（trace 缺 watch_score，无法排序）",
            ranked=False,
        )
    score = max(0.0, min(100.0, _PRE_BREAKOUT_SCORE_FLOOR + watch * 100.0))
    return ReviewShadowSignal(
        "pre_breakout", score, f"已通过L1-L3及{channel}，等待爆发前夜确认（watch_score={watch:.3f}）"
    )


def attach_shadow_signal(row: dict[str, Any], *, near_l2_max_gap_pct: float = NEAR_L2_MAX_GAP_PCT) -> dict[str, Any]:
    signal = shadow_signal_from_decision(row, near_l2_max_gap_pct=near_l2_max_gap_pct)
    if signal is None:
        return row
    return {
        **row,
        "shadow_lane": signal.lane,
        "shadow_score": signal.score,
        "shadow_ranked": signal.ranked,
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
