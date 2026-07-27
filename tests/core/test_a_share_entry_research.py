from core.a_share_entry_research import (
    AShareEntryResearchPolicy,
    calibrated_confirmation_score,
    confirmed_signal_allowed,
    entry_weight_multiplier,
    market_context_allows_entry,
    research_max_hold_days,
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


def test_neutral_spring_breadth_gate_only_blocks_unconfirmed_spring() -> None:
    policy = AShareEntryResearchPolicy(require_neutral_spring_breadth_confirmation=True)
    strong = {"ratio_pct": 55, "delta_pct": 2, "daily_up_ratio_pct": 60, "sample_size": 1000}

    assert not confirmed_signal_allowed(policy, "spring", "NEUTRAL", breadth={})
    assert confirmed_signal_allowed(policy, "spring", "NEUTRAL", breadth=strong)
    assert confirmed_signal_allowed(policy, "evr", "NEUTRAL", breadth={})
    assert confirmed_signal_allowed(policy, "spring", "CAUTION", breadth={})


def test_entry_weight_multiplier_only_changes_matching_regime_signal() -> None:
    policy = AShareEntryResearchPolicy(
        entry_weight_multipliers=(
            ("NEUTRAL", "spring", 0.5),
            ("CAUTION", "sos", 2.0),
        )
    )

    assert entry_weight_multiplier(policy, "SPRING", "neutral") == 0.5
    assert entry_weight_multiplier(policy, "sos", "CAUTION") == 1.0
    assert entry_weight_multiplier(policy, "evr", "NEUTRAL") == 1.0


def test_confirmed_signal_policy_can_block_one_regime_signal_pair() -> None:
    policy = AShareEntryResearchPolicy(blocked_confirmed_regime_signals=(("NEUTRAL", "spring"),))

    assert not confirmed_signal_allowed(policy, "spring", "neutral")
    assert confirmed_signal_allowed(policy, "spring", "CAUTION")
    assert confirmed_signal_allowed(policy, "sos", "NEUTRAL")


def test_research_max_hold_days_only_shortens_matching_regime_signal() -> None:
    policy = AShareEntryResearchPolicy(max_hold_days_by_regime_signal=(("CAUTION", "spring", 10),))

    assert research_max_hold_days(policy, "SPRING", "caution", 15) == 10
    assert research_max_hold_days(policy, "spring", "NEUTRAL", 15) == 15
    assert research_max_hold_days(policy, "spring", "CAUTION", 5) == 5


def test_empirical_score_caps_raw_strength_and_prioritizes_better_signal_families() -> None:
    policy = AShareEntryResearchPolicy(calibrate_confirmed_score=True)

    evr = calibrated_confirmation_score(policy, "evr", 100)
    spring = calibrated_confirmation_score(policy, "spring", 5)
    trend_pullback = calibrated_confirmation_score(policy, "trend_pullback", 5)

    assert trend_pullback > spring > evr
    assert calibrated_confirmation_score(AShareEntryResearchPolicy(), "evr", 100) == 100
