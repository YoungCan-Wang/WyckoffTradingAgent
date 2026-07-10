from __future__ import annotations

from core.candidate_selection_score import score_candidate_shadow


def test_candidate_shadow_score_rewards_confirmed_breakout_setup():
    score = score_candidate_shadow(
        signal_type="sos",
        trigger_score=12.0,
        priority_score=0.92,
        footprint={
            "bias": "demand",
            "tags": ["quality_breakout"],
            "negative_tags": [],
            "breakout_quality_score": 90,
            "absorption_score": 80,
            "dry_up_score": 60,
            "reclaim_score": 20,
            "supply_pressure_score": 10,
            "failed_breakout_score": 0,
        },
        springboard={
            "springboard_grade": "A+B+C",
            "springboard_met_count": 3,
            "springboard_a": True,
            "springboard_b": True,
            "springboard_c": True,
        },
        intraday_tail={"tail_score": 86, "tail_decision": "BUY", "dist_vwap_pct": 1.2},
        source_context={
            "lhb": {"net_buy": 1_000_000},
            "margin": {"margin_buy": 200_000, "margin_repay": 50_000},
            "tick_large_order": {"large_net_amount_yuan": 2_000_000},
        },
    )

    assert score["version"] == "candidate_shadow_score_v1"
    assert score["grade"] == "S"
    assert score["score"] >= 85
    assert score["components"]["funnel"] == 27.6
    assert score["components"]["springboard"] == 18.0
    assert "quality_breakout" in score["positive_tags"]
    assert "springboard_confirmed" in score["positive_tags"]
    assert "tail_buy_confirmation" in score["positive_tags"]
    assert "lhb_net_buy" in score["positive_tags"]
    assert score["negative_tags"] == []


def test_candidate_shadow_score_penalizes_failed_breakout_supply():
    score = score_candidate_shadow(
        signal_type="sos",
        trigger_score=16.0,
        priority_score=80.0,
        footprint={
            "bias": "supply",
            "tags": [],
            "negative_tags": ["failed_breakout", "weak_close"],
            "breakout_quality_score": 10,
            "absorption_score": 20,
            "dry_up_score": 0,
            "reclaim_score": 0,
            "supply_pressure_score": 95,
            "failed_breakout_score": 90,
        },
        intraday_tail={"tail_score": 30, "tail_decision": "SKIP", "dist_vwap_pct": -1.4},
        source_context={
            "lhb": {"net_buy": -500_000},
            "block_trade": {"total_amount": 2_000_000, "avg_discount_pct": -5.0},
            "tick_large_order": {"large_net_amount_yuan": -1_500_000},
        },
    )

    assert score["grade"] == "D"
    assert score["components"]["risk_penalty"] == -20.0
    assert "supply_pressure" in score["negative_tags"]
    assert "failed_breakout" in score["negative_tags"]
    assert "tail_skip" in score["negative_tags"]
    assert "below_vwap" in score["negative_tags"]
    assert "large_order_net_sell" in score["negative_tags"]


def test_candidate_shadow_score_falls_back_to_trigger_score_only():
    score = score_candidate_shadow(signal_type="spring", trigger_score=10.0)

    assert score["score"] == 15.0
    assert score["grade"] == "D"
    assert score["components"] == {
        "funnel": 15.0,
        "price_action": 0.0,
        "springboard": 0.0,
        "tail_confirmation": 0.0,
        "external_capital": 0.0,
        "risk_penalty": 0.0,
    }
    assert score["score_inputs"]["trigger_score"] == 10.0


def test_candidate_shadow_score_sanitizes_nonfinite_inputs():
    score = score_candidate_shadow(
        signal_type="sos",
        trigger_score=float("inf"),
        priority_score=float("-inf"),
        footprint={"breakout_quality_score": "Infinity", "supply_pressure_score": float("inf")},
        intraday_tail={"tail_score": float("nan"), "dist_vwap_pct": float("-inf")},
        source_context={
            "lhb": {"net_buy": float("inf")},
            "margin": {"margin_buy": "Infinity", "margin_repay": 1},
            "tick_large_order": {"large_net_amount_yuan": float("-inf")},
        },
    )

    assert score["score"] == 0.0
    assert score["components"] == {
        "funnel": 0.0,
        "price_action": 0.0,
        "springboard": 0.0,
        "tail_confirmation": 0.0,
        "external_capital": 0.0,
        "risk_penalty": 0.0,
    }
    assert score["score_inputs"] == {"trigger_score": 0.0, "priority_score": 0.0}
    assert score["positive_tags"] == []
    assert score["negative_tags"] == []
