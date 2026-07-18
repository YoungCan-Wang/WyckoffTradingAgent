from core.a_share_entry_research import (
    AShareEntryResearchPolicy,
    calibrated_confirmation_score,
    confirmed_signal_allowed,
    market_context_allows_entry,
)


def test_blocked_confirmed_signal_is_not_tradeable() -> None:
    policy = AShareEntryResearchPolicy(blocked_confirmed_signals=("evr", "sos"))

    assert not confirmed_signal_allowed(policy, "EVR")
    assert not confirmed_signal_allowed(policy, "sos")
    assert confirmed_signal_allowed(policy, "spring")


def test_neutral_breadth_gate_fails_closed_but_does_not_replace_other_regimes() -> None:
    policy = AShareEntryResearchPolicy(require_neutral_breadth_confirmation=True)
    strong = {"ratio_pct": 55, "delta_pct": 2, "daily_up_ratio_pct": 60, "sample_size": 1000}

    assert market_context_allows_entry(policy, regime="NEUTRAL", breadth=strong)
    assert not market_context_allows_entry(policy, regime="NEUTRAL", breadth={})
    assert market_context_allows_entry(policy, regime="CAUTION", breadth={})


def test_empirical_score_caps_raw_strength_and_prioritizes_better_signal_families() -> None:
    policy = AShareEntryResearchPolicy(calibrate_confirmed_score=True)

    evr = calibrated_confirmation_score(policy, "evr", 100)
    spring = calibrated_confirmation_score(policy, "spring", 5)
    trend_pullback = calibrated_confirmation_score(policy, "trend_pullback", 5)

    assert trend_pullback > spring > evr
    assert calibrated_confirmation_score(AShareEntryResearchPolicy(), "evr", 100) == 100
