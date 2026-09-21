"""Tests for candidate lane pruning mechanisms and variant Q configuration."""

from __future__ import annotations

import pandas as pd

from core.candidate_lanes import build_l1_candidate_lane_entries, merge_candidate_entries
from core.wyckoff_engine import (
    FunnelConfig,
    _alpha_entry_options,
    _formal_candidate_entries,
    build_candidate_entries,
)
from workflows.backtest_strategy_variants import (
    normalize_strategy_variant,
    strategy_variant_entry_policy,
    strategy_variant_overrides,
)


def test_funnel_config_blocked_candidate_entry_types_default_is_empty():
    cfg = FunnelConfig()
    assert cfg.blocked_candidate_entry_types == ()


def test_alpha_entry_options_respects_blocked_candidate_entry_types():
    row = {
        "code": "000001",
        "close": 10.0,
        "high": 10.2,
        "low": 9.8,
        "ma20": 9.9,
        "ma50": 9.5,
        "ret20": 10.0,
        "ret60": 20.0,
        "ret120": 30.0,
        "range20": 8.0,
        "range60": 15.0,
        "range120": 25.0,
        "vol_ratio_5_20": 1.1,
        "pos_in_range20": 0.8,
        "pos_in_range60": 0.8,
        "near_high120_pct": -2.0,
        "ma50_slope20": 1.5,
        "price_low_pct": 12.0,
        "above_ma20": True,
        "above_ma50": True,
        "drawdown_from_high": 3.0,
    }
    cfg_live = FunnelConfig()
    options_live = _alpha_entry_options(row, cfg_live)
    live_types = {item[1] for item in options_live}
    assert "launchpad" in live_types

    cfg_pruned = FunnelConfig(blocked_candidate_entry_types=("launchpad",))
    options_pruned = _alpha_entry_options(row, cfg_pruned)
    pruned_types = {item[1] for item in options_pruned}
    assert "launchpad" not in pruned_types
    # Non-blocked options remain intact
    for item in options_pruned:
        assert item[1] != "launchpad"


def test_formal_candidate_entries_respects_blocked_candidate_entry_types():
    triggers = {
        "spring": [("000001", 10.0)],
        "trend_pullback": [("000002", 8.0)],
    }
    stage_map = {"000001": "Accum_C", "000002": "Markup"}
    exit_signals = {}

    entries_all = _formal_candidate_entries(triggers, stage_map, exit_signals)
    assert {e["entry_type"] for e in entries_all} == {"spring", "trend_pullback"}

    entries_filtered = _formal_candidate_entries(triggers, stage_map, exit_signals, blocked_types=("trend_pullback",))
    assert {e["entry_type"] for e in entries_filtered} == {"spring"}


def test_build_candidate_entries_filters_blocked_types():
    df = pd.DataFrame(
        {
            "close": [10.0] * 300,
            "open": [10.0] * 300,
            "high": [10.2] * 300,
            "low": [9.8] * 300,
            "volume": [1000] * 300,
            "amount": [10000] * 300,
        }
    )
    triggers = {"spring": [("000001", 10.0)]}
    cfg = FunnelConfig(blocked_candidate_entry_types=("spring",))
    entries = build_candidate_entries(
        alpha_symbols=["000001"],
        df_map={"000001": df},
        sector_map={"000001": "Tech"},
        channel_map={"000001": "momentum"},
        triggers=triggers,
        stage_map={"000001": "Accum_C"},
        exit_signals={},
        cfg=cfg,
    )
    assert all(e["entry_type"] != "spring" for e in entries)


def test_build_l1_candidate_lane_entries_and_merge_filtering():
    def _make_df(close_trend):
        dates = pd.date_range("2024-01-01", periods=len(close_trend))
        return pd.DataFrame(
            {
                "date": dates,
                "close": close_trend,
                "open": close_trend,
                "high": [c * 1.02 for c in close_trend],
                "low": [c * 0.98 for c in close_trend],
                "volume": [100000] * len(close_trend),
                "amount": [1000000] * len(close_trend),
            }
        )

    # Steady upward trend to satisfy lane conditions
    closes = [10.0 + i * 0.1 for i in range(250)]
    df = _make_df(closes)

    lanes_all = build_l1_candidate_lane_entries(
        l1_symbols=["000001"],
        df_map={"000001": df},
        sector_map={"000001": "Tech"},
        top_sectors=["Tech"],
        l2_symbols=["000001"],
        channel_map={"000001": "markup_track"},
    )
    assert len(lanes_all) > 0
    chosen_lane = lanes_all[0]["entry_type"]

    # Block the chosen lane -> either falls through or returns empty
    lanes_pruned = build_l1_candidate_lane_entries(
        l1_symbols=["000001"],
        df_map={"000001": df},
        sector_map={"000001": "Tech"},
        top_sectors=["Tech"],
        l2_symbols=["000001"],
        channel_map={"000001": "markup_track"},
        blocked_lanes=(chosen_lane,),
    )
    assert all(item["entry_type"] != chosen_lane for item in lanes_pruned)

    # Test merge_candidate_entries filtering
    merged = merge_candidate_entries(lanes_all, blocked_types=(chosen_lane,))
    assert all(item["entry_type"] != chosen_lane for item in merged)


def test_strategy_variant_q_clean_isolation():
    variant = normalize_strategy_variant("Q")
    assert variant == "Q"

    overrides = strategy_variant_overrides("Q")
    # Live parity switches must be explicitly True, NOT flipped to False like _ALL_SWITCHES
    assert overrides["dist_upthrust_enabled"] is True
    assert overrides["lps_creek_confirmation_enabled"] is True
    # Target 6 channels are blocked
    blocked = overrides["blocked_candidate_entry_types"]
    assert set(blocked) == {
        "launchpad",
        "early_breakout",
        "volatile_pullback",
        "trend_lane_pullback",
        "trend_breakout",
        "main_force_entry",
    }

    # Zero policy distortion on entry weights or confirmation rules
    policy = strategy_variant_entry_policy("Q")
    assert policy.blocked_confirmed_signals == ()
    assert policy.entry_weight_multipliers == ()
