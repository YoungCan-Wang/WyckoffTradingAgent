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
    compression = calibrated_confirmation_score(policy, "compression", 5)
    trend_pullback = calibrated_confirmation_score(policy, "trend_pullback", 5)

    # 强原始分的差信号不得超过弱原始分的好信号：evr 即使满强度也压不过
    # compression / trend_pullback 的低强度确认。
    assert trend_pullback > compression > evr
    # spring prior 已按实测下调到 0.15（低于 evr 的 0.10 + 满强度），故不再
    # 参与"prior 高者胜"的比较；它只需低于 compression / trend_pullback。
    assert compression > spring
    assert calibrated_confirmation_score(AShareEntryResearchPolicy(), "evr", 100) == 100


def test_spring_prior_reflects_measured_underperformance() -> None:
    """spring prior 0.65 → 0.15：它是唯一样本充足且五周期方向全负的类型。

    run 31293167694（3594 笔、五个市场环境）：spring -3.15%/n=1285，逐周期
    -4.66 / -4.45 / -4.68 / -1.62 / -2.68（均值标准差仅 1.39），对照其余类型
    Welch t=-4.26。原 prior 0.65 让它排第四高，与实测末位不符。

    未调 trend_pullback / compression / lps：前者逐周期方向不稳（标准差 8.39），
    后两者样本仅 30 / 11 笔。
    """
    from core.a_share_entry_research import CONFIRMED_SIGNAL_PRIOR

    assert CONFIRMED_SIGNAL_PRIOR["spring"] == 0.15
    # spring 应低于所有 prior 未变的形态，仅高于 evr
    assert CONFIRMED_SIGNAL_PRIOR["spring"] < CONFIRMED_SIGNAL_PRIOR["sos"]
    assert CONFIRMED_SIGNAL_PRIOR["spring"] < CONFIRMED_SIGNAL_PRIOR["compression"]
    assert CONFIRMED_SIGNAL_PRIOR["spring"] < CONFIRMED_SIGNAL_PRIOR["trend_pullback"]
    assert CONFIRMED_SIGNAL_PRIOR["spring"] > CONFIRMED_SIGNAL_PRIOR["evr"]
