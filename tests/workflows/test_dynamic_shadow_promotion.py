from __future__ import annotations

from workflows.dynamic_shadow_promotion import _score_rows


def test_score_rows_uses_regime_health_and_returns_promotion_eligibility(monkeypatch) -> None:
    monkeypatch.setenv("FUNNEL_DYNAMIC_SHADOW_MIN_SCORE", "60")
    details = {
        "review_triggers": {"sos": [("000001", 90.0)]},
        "priority_score_map": {"000001": 90.0},
        "springboard_map": {
            "sos:000001": {
                "springboard_met_count": 3,
                "springboard_a": True,
                "springboard_b": True,
                "springboard_c": True,
            }
        },
        "footprint_map": {
            "sos:000001": {
                "bias": "demand",
                "breakout_quality_score": 90,
                "absorption_score": 85,
                "dry_up_score": 80,
                "reclaim_score": 80,
            }
        },
        "source_context_map": {"000001": {"stock_moneyflow": {"net_amount_wan": 100.0}}},
        "dynamic_shadow_health_map": {
            ("sos", "NEUTRAL"): {
                "health_state": "HEALTHY",
                "sample_count": 40,
                "weight_multiplier": 1.0,
                "regime": "NEUTRAL",
                "horizon_days": 5,
            }
        },
    }

    rows = _score_rows(details, "NEUTRAL")

    assert rows[0]["code"] == "000001"
    assert rows[0]["dynamic_score"] > rows[0]["base_score"]
    assert rows[0]["promotion"]["eligible"] is True


def test_score_rows_does_not_promote_insufficient_signal_history(monkeypatch) -> None:
    monkeypatch.setenv("FUNNEL_DYNAMIC_SHADOW_MIN_SCORE", "50")
    details = {
        "review_triggers": {"lps": [("000001", 90.0)]},
        "springboard_map": {"lps:000001": {"springboard_met_count": 2}},
        "footprint_map": {"lps:000001": {"absorption_score": 90, "dry_up_score": 90}},
        "dynamic_shadow_health_map": {
            ("lps", "NEUTRAL"): {
                "health_state": "INSUFFICIENT",
                "sample_count": 12,
                "weight_multiplier": 0.6,
            }
        },
    }

    rows = _score_rows(details, "NEUTRAL")

    assert rows[0]["promotion"]["eligible"] is False
    assert "signal_health" in rows[0]["promotion"]["blockers"]
