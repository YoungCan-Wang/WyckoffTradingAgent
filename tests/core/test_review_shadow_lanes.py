from core.funnel_taxonomy import REVIEW_STAGE_RISK_BLOCK, REVIEW_STAGE_STRENGTH_MISS, REVIEW_STAGE_THEME_MISS
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
