from workflows.backtest_strategy_variants import (
    DEFAULT_COMPARISON_VARIANTS,
    normalize_strategy_variant,
    strategy_variant_entry_policy,
    strategy_variant_overrides,
)


def test_strategy_variants_isolate_each_research_switch() -> None:
    baseline = strategy_variant_overrides("A")
    assert not any(baseline.values())
    assert strategy_variant_overrides("B")["dist_upthrust_enabled"] is True
    assert strategy_variant_overrides("C")["regime_trigger_profiles_enabled"] is True
    assert strategy_variant_overrides("D") == {
        "dist_upthrust_enabled": False,
        "regime_trigger_profiles_enabled": False,
        "lps_creek_confirmation_enabled": True,
        "signal_sequence_bonus_enabled": True,
    }
    assert all(strategy_variant_overrides("E").values())
    assert DEFAULT_COMPARISON_VARIANTS == ("A", "F", "G", "H", "I")
    assert strategy_variant_overrides("F") == baseline
    assert strategy_variant_entry_policy("F").blocked_confirmed_signals == ("evr",)
    assert strategy_variant_entry_policy("G").blocked_confirmed_signals == ("evr", "sos")
    assert strategy_variant_entry_policy("H").require_neutral_breadth_confirmation is True
    assert strategy_variant_entry_policy("I").calibrate_confirmed_score is True


def test_live_variant_preserves_production_configuration() -> None:
    assert normalize_strategy_variant("live") == "live"
    assert strategy_variant_overrides("live") == {}
    assert strategy_variant_entry_policy("live").blocked_confirmed_signals == ()
