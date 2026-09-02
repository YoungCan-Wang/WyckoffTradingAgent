from core.funnel_taxonomy import (
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_MISS,
)
from core.review_shadow_lanes import shadow_signal_from_decision


def test_near_l2_requires_positive_bounded_gap() -> None:
    signal = shadow_signal_from_decision(
        {"stage": REVIEW_STAGE_STRENGTH_MISS, "reason": "最接近通道[主升](缺口6.3%): RPS不足"}
    )

    assert signal is not None
    assert signal.lane == "near_l2"
    assert (
        shadow_signal_from_decision({"stage": REVIEW_STAGE_STRENGTH_MISS, "reason": "最接近通道[主升](缺口0.0%): "})
        is None
    )


def test_rotation_lane_requires_l2_and_risk_block_never_promotes() -> None:
    signal = shadow_signal_from_decision(
        {"stage": REVIEW_STAGE_THEME_MISS, "l2_eligible": True, "l2_channel": "主升通道"}
    )

    assert signal is not None and signal.lane == "rotation_setup"
    assert shadow_signal_from_decision({"stage": REVIEW_STAGE_RISK_BLOCK, "l3_eligible": True}) is None


def test_rotation_setup_is_a_tag_not_a_score() -> None:
    """题材共振层没有连续键,就不要造一个。

    v1 给 rotation_setup 恒定 72.0,读着像分其实是标签,拿去跟 near_l2 的真分比
    就是在比两个不同量纲的东西。layer3_sector_resonance 是集合成员判断
    (top_sector_set/keep_sector_set/hot_set),压根没有缺口指标可拿。
    """
    signal = shadow_signal_from_decision(
        {"stage": REVIEW_STAGE_THEME_MISS, "l2_eligible": True, "l2_channel": "主升通道"}
    )

    assert signal is not None
    assert signal.score is None
    assert signal.ranked is False
    assert "无连续排序键" in signal.reason


def test_pre_breakout_score_tracks_watch_score() -> None:
    """pre_breakout 的分要随 watch_score 变,不能像 v1 那样恒为 78.0。

    2026-09-01 那份复盘的 31 只「买点未确认」在 v1 下全同分——同分排不了序,
    「取前 N 只看超额」就无从下手。watch_score 是漏斗自己在 rank_l3_candidates
    里对 L3 全体算过的连续键,取值约 [0,1]。
    """
    base = {"stage": REVIEW_STAGE_TRIGGER_MISS, "l3_eligible": True, "l2_channel": "主升通道"}
    low = shadow_signal_from_decision({**base, "layer3_quality_score": 0.12})
    high = shadow_signal_from_decision({**base, "layer3_quality_score": 0.61})

    assert low is not None and high is not None
    assert low.lane == high.lane == "pre_breakout"
    assert low.ranked is True and high.ranked is True
    assert low.score is not None and high.score is not None
    assert high.score > low.score
    # 与 near_l2 同量纲(0~100),否则跨车道分档没法比。
    assert 0.0 <= low.score <= 100.0 and 0.0 <= high.score <= 100.0
    assert "watch_score=0.610" in high.reason


def test_pre_breakout_without_watch_score_degrades_to_unranked() -> None:
    """老 trace 没有 watch_score 字段时显式说排不了序,而不是用常数假装能排。"""
    signal = shadow_signal_from_decision(
        {"stage": REVIEW_STAGE_TRIGGER_MISS, "l3_eligible": True, "l2_channel": "主升通道"}
    )

    assert signal is not None and signal.lane == "pre_breakout"
    assert signal.score is None
    assert signal.ranked is False
    assert "无法排序" in signal.reason
