from workflows.backtest_strategy_variants import (
    DEFAULT_COMPARISON_VARIANTS,
    normalize_strategy_variant,
    strategy_variant_entry_policy,
    strategy_variant_overrides,
    strategy_variants_share_signal_ledger,
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
        "lps_use_fib_zone": False,
        "lps_creek_dynamic_relax": False,
        "enable_market_regime_gate": False,
    }
    assert all(strategy_variant_overrides("E").values())
    assert strategy_variant_overrides("K")["lps_use_fib_zone"] is True
    assert strategy_variant_overrides("K")["lps_creek_dynamic_relax"] is True
    assert DEFAULT_COMPARISON_VARIANTS == ("A", "M", "P")
    assert strategy_variant_overrides("F") == baseline
    assert strategy_variant_entry_policy("F").blocked_confirmed_signals == ("evr",)
    assert strategy_variant_entry_policy("G").blocked_confirmed_signals == ("evr", "sos")
    assert strategy_variant_entry_policy("H").require_neutral_breadth_confirmation is True
    assert strategy_variant_entry_policy("I").calibrate_confirmed_score is True
    assert strategy_variant_entry_policy("P").entry_weight_multipliers[0] == ("NEUTRAL", "spring", 0.25)
    assert strategy_variant_overrides("R")["enable_layer3"] is False
    assert strategy_variant_overrides("R")["dist_upthrust_enabled"] is True
    assert strategy_variant_overrides("R")["lps_creek_confirmation_enabled"] is True
    assert strategy_variants_share_signal_ledger(["A", "M", "P"]) is True
    assert strategy_variants_share_signal_ledger(["A", "F"]) is False
    assert strategy_variants_share_signal_ledger(["A", "I"]) is False


def test_live_variant_preserves_production_configuration() -> None:
    assert normalize_strategy_variant("live") == "live"
    assert strategy_variant_overrides("live") == {}
    assert strategy_variant_entry_policy("live").blocked_confirmed_signals == ()


def test_knockouts_only_change_one_live_switch():
    from dataclasses import asdict, replace

    from core.wyckoff_engine import FunnelConfig
    from workflows.backtest_strategy_variants import LIVE_KNOCKOUT_SWITCHES

    base = FunnelConfig()
    for variant, field in LIVE_KNOCKOUT_SWITCHES.items():
        assert getattr(base, field) is True
        overrides = strategy_variant_overrides(variant)
        assert overrides == {field: False}
        changed = asdict(replace(base, **overrides))
        assert [key for key, value in asdict(base).items() if changed[key] != value] == [field]
        assert strategy_variant_entry_policy(variant) == strategy_variant_entry_policy("live")
        assert not strategy_variants_share_signal_ledger(["live", variant])


def test_combo_knockouts_only_change_targeted_live_switches() -> None:
    from dataclasses import asdict, replace

    from core.wyckoff_engine import FunnelConfig
    from workflows.backtest_strategy_variants import LIVE_COMBO_KNOCKOUT_SWITCHES

    base = FunnelConfig()
    for variant, fields in LIVE_COMBO_KNOCKOUT_SWITCHES.items():
        for field in fields:
            assert getattr(base, field) is True
        overrides = strategy_variant_overrides(variant)
        assert overrides == {field: False for field in fields}
        changed = asdict(replace(base, **overrides))
        assert sorted(key for key, value in asdict(base).items() if changed[key] != value) == sorted(fields)
        assert strategy_variant_entry_policy(variant) == strategy_variant_entry_policy("live")
        assert not strategy_variants_share_signal_ledger(["live", variant])
