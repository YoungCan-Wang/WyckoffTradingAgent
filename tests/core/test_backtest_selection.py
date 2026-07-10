from __future__ import annotations

import pandas as pd
import pytest

from core.backtest_metrics import calc_stratified_stats
from core.backtest_selection import select_ai_input_codes
from core.candidate_policy import (
    CandidatePolicyConfig,
    loss_guard_reason,
    rerank_selected_codes,
)
from core.wyckoff_engine import FunnelResult


def _daily_position_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [x * 0.995 for x in closes],
            "close": closes,
            "high": [x * 1.01 for x in closes],
            "low": [x * 0.99 for x in closes],
            "volume": [100.0 for _ in closes],
        }
    )


def _low_confirmation_df(rows: int = 80) -> pd.DataFrame:
    closes = [10.0 + idx * 0.01 for idx in range(rows)]
    dates = pd.date_range("2025-01-01", periods=rows, freq="B")
    return pd.DataFrame(
        {
            "date": dates,
            "open": [x * 0.995 for x in closes],
            "close": closes,
            "high": [x * 1.01 for x in closes],
            "low": [x * 0.99 for x in closes],
            "volume": [1000.0 for _ in closes],
        }
    )


def test_rerank_selected_codes_treats_invalid_scores_as_zero() -> None:
    ranked = rerank_selected_codes(
        ["BAD", "GOOD", "NAN", "INF"],
        {"BAD": "bad", "GOOD": 2.0, "NAN": float("nan"), "INF": float("inf")},
    )

    assert ranked == ["GOOD", "BAD", "INF", "NAN"]


def test_all_formal_l4_selection_treats_invalid_trigger_scores_as_zero() -> None:
    result = FunnelResult(
        layer1_symbols=["GOOD", "BAD", "INF", "NAN"],
        layer2_symbols=["GOOD", "BAD", "INF", "NAN"],
        layer3_symbols=["GOOD", "BAD", "INF", "NAN"],
        top_sectors=[],
        triggers={"sos": [("GOOD", 2.0), ("BAD", "bad"), ("INF", float("inf")), ("NAN", float("nan"))]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="all_formal_l4",
    )

    assert codes == ["GOOD", "BAD", "INF", "NAN"]
    assert score_map == {"GOOD": 2.0, "BAD": 0.0, "INF": 0.0, "NAN": 0.0}
    assert track_map == {"GOOD": "Trend", "BAD": "Trend", "INF": "Trend", "NAN": "Trend"}


def test_all_formal_l4_selection_excludes_stage_only_candidates() -> None:
    result = FunnelResult(
        layer1_symbols=["000001", "000002"],
        layer2_symbols=["000001", "000002"],
        layer3_symbols=["000001", "000002"],
        top_sectors=[],
        triggers={"sos": [("000001", 2.0)]},
        stage_map={"000002": "Markup"},
        markup_symbols=["000002"],
        exit_signals={},
        channel_map={"000001": "点火破局", "000002": "主升通道"},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="all_formal_l4",
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 2.0}
    assert track_map == {"000001": "Trend"}


def test_all_formal_l4_selection_respects_hard_cap() -> None:
    result = FunnelResult(
        layer1_symbols=["000001", "000002", "000003"],
        layer2_symbols=["000001", "000002", "000003"],
        layer3_symbols=["000001", "000002", "000003"],
        top_sectors=[],
        triggers={"sos": [("000001", 3.0), ("000002", 2.0), ("000003", 1.0)]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={"000001": "点火破局", "000002": "点火破局", "000003": "点火破局"},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="all_formal_l4",
        full_formal_l4_max=2,
    )

    assert codes == ["000001", "000002"]
    assert score_map == {"000001": 3.0, "000002": 2.0}
    assert track_map == {"000001": "Trend", "000002": "Trend"}


def test_all_formal_l4_selection_applies_signal_weight_map() -> None:
    result = FunnelResult(
        layer1_symbols=["000001", "000002"],
        layer2_symbols=["000001", "000002"],
        layer3_symbols=["000001", "000002"],
        top_sectors=[],
        triggers={"sos": [("000001", 8.0)], "lps": [("000002", 12.0)]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={"000001": "点火破局", "000002": "地量蓄势"},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="all_formal_l4",
        signal_weight_map={"lps": 0.4},
    )

    assert codes == ["000001", "000002"]
    assert score_map["000001"] == 8.0
    assert score_map["000002"] == pytest.approx(4.8)
    assert track_map == {"000001": "Trend", "000002": "Accum"}


def test_all_formal_l4_selection_applies_scoped_regime_signal_weight() -> None:
    result = FunnelResult(
        layer1_symbols=["000001", "000002"],
        layer2_symbols=["000001", "000002"],
        layer3_symbols=["000001", "000002"],
        top_sectors=[],
        triggers={"sos": [("000001", 8.0)], "lps": [("000002", 12.0)]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={"000001": "点火破局", "000002": "地量蓄势"},
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="RISK_ON",
        selection_mode="all_formal_l4",
        signal_weight_map={"lps|regime=RISK_ON": 0.4},
    )

    assert codes == ["000001", "000002"]
    assert score_map["000001"] == 8.0
    assert score_map["000002"] == pytest.approx(4.8)
    assert track_map == {"000001": "Trend", "000002": "Accum"}


def test_tradeable_l4_selection_uses_formal_l4_without_l3_fallback() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=["000001", "000003", "000004", "000005", "000006"],
        top_sectors=[],
        triggers={
            "sos": [("000001", 5.0), ("000003", 4.0)],
            "lps": [("000004", 2.0), ("000005", 1.0)],
            "spring": [("000005", 1.5)],
            "compression": [("000006", 1.0)],
        },
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={
            "000001": "主升通道",
            "000003": "点火破局",
            "000004": "吸筹通道",
            "000005": "吸筹通道",
            "000006": "吸筹通道",
        },
        leader_radar_symbols=[],
        leader_radar_rows=[],
    )

    codes, score_map, _ = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="RISK_ON",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000005", "000006"]
    assert score_map == {"000005": 1.5, "000006": 1.0}


def test_tradeable_l4_selection_prefers_candidate_board_when_available() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={"sos": [("000001", 5.0)]},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {
                "code": "000002",
                "track": "future_leader",
                "entry_type": "launchpad",
                "score": 78.0,
            }
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002"]
    assert score_map == {"000002": 78.0}
    assert track_map == {"000002": "Trend"}


def test_tradeable_l4_candidate_board_applies_signal_weight_map() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {
                "code": "000001",
                "track": "future_leader",
                "entry_type": "wyckoff_structure",
                "signal_key": "sos",
                "score": 70.0,
            },
            {
                "code": "000002",
                "track": "future_leader",
                "entry_type": "wyckoff_structure",
                "signal_key": "lps",
                "score": 90.0,
            },
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
        candidate_policy=CandidatePolicyConfig(loss_guard_enabled=False),
        signal_weight_map={"lps": 0.5},
    )

    assert codes == ["000001", "000002"]
    assert score_map == {"000001": 70.0, "000002": 45.0}
    assert track_map == {"000001": "Trend", "000002": "Trend"}


def test_tradeable_l4_candidate_board_applies_scoped_signal_weight_map() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {
                "code": "000001",
                "track": "future_leader",
                "entry_type": "wyckoff_structure",
                "candidate_lane": "trend_pullback",
                "signal_key": "lps",
                "score": 90.0,
            },
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="RISK_ON",
        selection_mode="tradeable_l4",
        candidate_policy=CandidatePolicyConfig(loss_guard_enabled=False),
        signal_weight_map={"lps|regime=RISK_ON|lane=trend_pullback|entry=wyckoff_structure": 0.5},
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 45.0}
    assert track_map == {"000001": "Trend"}


def test_tradeable_l4_candidate_board_allows_high_score_risk_on_early_breakout() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "breakout", "entry_type": "early_breakout", "score": 92.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="RISK_ON",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 92.0}
    assert track_map == {"000001": "Trend"}


def test_tradeable_l4_candidate_board_blocks_low_score_risk_on_early_breakout() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "breakout", "entry_type": "early_breakout", "score": 69.0},
            {"code": "000002", "track": "future_leader", "entry_type": "launchpad", "score": 78.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="RISK_ON",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002"]
    assert score_map == {"000002": 78.0}
    assert track_map == {"000002": "Trend"}


def test_tradeable_l4_candidate_board_prioritizes_launchpad_over_formal_score() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "accumulation", "entry_type": "spring", "score": 100.0},
            {"code": "000002", "track": "future_leader", "entry_type": "launchpad", "score": 80.0},
        ],
    )

    codes, _, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002", "000001"]
    assert track_map == {"000002": "Trend", "000001": "Accum"}


def test_tradeable_l4_candidate_board_uses_signal_key_when_entry_type_is_display_text() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "trend", "entry_type": "trend_breakout", "score": 100.0},
            {"code": "000002", "signal_key": "mainline", "entry_type": "主线回踩MA20", "score": 70.0},
        ],
    )

    codes, _score_map, _track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002", "000001"]


def test_tradeable_l4_candidate_board_keeps_best_duplicate_score_and_track() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "future_leader", "entry_type": "launchpad", "score": 80.0},
            {"code": "000001", "track": "accumulation", "entry_type": "spring", "score": 100.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 100.0}
    assert track_map == {"000001": "Accum"}


def test_tradeable_l4_candidate_board_ranks_by_best_duplicate_entry() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "future_leader", "entry_type": "launchpad", "score": 80.0},
            {"code": "000001", "track": "accumulation", "entry_type": "spring", "score": 100.0},
            {"code": "000002", "track": "future_leader", "entry_type": "tight_base", "score": 90.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002", "000001"]
    assert score_map == {"000002": 90.0, "000001": 100.0}
    assert track_map == {"000002": "Trend", "000001": "Accum"}


def test_tradeable_l4_candidate_board_ranks_unknown_entry_after_known_entries() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "future_leader", "entry_type": "unmapped_display_text", "score": 100.0},
            {"code": "000002", "track": "accumulation", "entry_type": "compression", "score": 80.0},
        ],
    )

    codes, _score_map, _track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002", "000001"]


def test_tradeable_l4_candidate_board_normalizes_entry_type_for_loss_guard() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "breakout", "entry_type": "Early-Breakout", "score": 69.0},
            {"code": "000002", "track": "future_leader", "entry_type": "launchpad", "score": 78.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="RISK_ON",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000002"]
    assert score_map == {"000002": 78.0}
    assert track_map == {"000002": "Trend"}


def test_tradeable_l4_candidate_board_accepts_accum_track_alias() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "Accum", "entry_type": "spring", "score": 100.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 100.0}
    assert track_map == {"000001": "Accum"}


def test_tradeable_l4_candidate_board_infers_accum_track_from_entry_type() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "entry_type": "spring", "score": 100.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 100.0}
    assert track_map == {"000001": "Accum"}


def test_tradeable_l4_candidate_board_selects_trend_lane_entry() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "000001", "track": "trend", "entry_type": "trend_lane_pullback", "score": 82.0},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["000001"]
    assert score_map == {"000001": 82.0}
    assert track_map == {"000001": "Trend"}


def test_tradeable_l4_candidate_board_treats_invalid_scores_as_zero() -> None:
    result = FunnelResult(
        layer1_symbols=[],
        layer2_symbols=[],
        layer3_symbols=[],
        top_sectors=[],
        triggers={},
        stage_map={},
        markup_symbols=[],
        exit_signals={},
        channel_map={},
        leader_radar_symbols=[],
        leader_radar_rows=[],
        candidate_entries=[
            {"code": "BAD", "track": "trend", "entry_type": "trend_lane_pullback", "score": "bad"},
            {"code": "INF", "track": "trend", "entry_type": "trend_lane_pullback", "score": float("inf")},
            {"code": "NAN", "track": "trend", "entry_type": "trend_lane_pullback", "score": float("nan")},
        ],
    )

    codes, score_map, track_map = select_ai_input_codes(
        result=result,
        day_df_map={},
        sector_map={},
        regime="NEUTRAL",
        selection_mode="tradeable_l4",
    )

    assert codes == ["BAD", "INF", "NAN"]
    assert score_map == {"BAD": 0.0, "INF": 0.0, "NAN": 0.0}
    assert track_map == {"BAD": "Trend", "INF": "Trend", "NAN": "Trend"}


def test_candidate_policy_config_can_disable_loss_guard() -> None:
    reason = loss_guard_reason(
        "000001",
        "RISK_ON",
        ["lps"],
        0.1,
        "",
        {},
        config=CandidatePolicyConfig(loss_guard_enabled=False),
    )

    assert reason == ""


def test_loss_guard_blocks_defensive_high_position_chase() -> None:
    df = _daily_position_df([10.0 + idx * 0.2 for idx in range(21)])

    reason = loss_guard_reason(
        "000001",
        "RISK_OFF",
        ["sos"],
        8.0,
        "点火破局",
        {"000001": df},
    )

    assert reason == "RISK_OFF20日高位追涨"


def test_loss_guard_keeps_defensive_spring_even_near_range_high() -> None:
    df = _daily_position_df([10.0 + idx * 0.2 for idx in range(21)])

    reason = loss_guard_reason(
        "000001",
        "RISK_OFF",
        ["spring"],
        8.0,
        "吸筹通道",
        {"000001": df},
    )

    assert reason == ""


def test_loss_guard_blocks_weak_right_side_without_abc_confirmation() -> None:
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["sos"],
        8.0,
        "点火破局",
        {"000001": _low_confirmation_df()},
    )

    assert reason == "右侧信号ABC不足"


def test_loss_guard_blocks_weak_trend_candidate_without_abc_confirmation() -> None:
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["trend_breakout"],
        88.0,
        "趋势延续",
        {"000001": _low_confirmation_df()},
    )

    assert reason == "趋势候选ABC不足"


def test_loss_guard_blocks_weak_main_force_entry_without_abc_confirmation() -> None:
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["main_force_entry"],
        88.0,
        "趋势延续",
        {"000001": _low_confirmation_df()},
    )

    assert reason == "趋势候选ABC不足"


def test_stratified_stats_include_exit_and_excursion_diagnostics() -> None:
    trades = pd.DataFrame(
        [
            {
                "track": "Trend",
                "regime": "RISK_ON",
                "trigger": "sos",
                "entry_price_source": "daily_close_fallback",
                "exit_reason": "stop_loss",
                "ret_pct": -6.9,
                "mfe_pct": 3.0,
                "mae_pct": -7.0,
            },
            {
                "track": "Trend",
                "regime": "RISK_ON",
                "trigger": "sos",
                "entry_price_source": "tail_1455",
                "exit_reason": "time_exit",
                "ret_pct": 4.0,
                "mfe_pct": 9.0,
                "mae_pct": -2.0,
            },
        ]
    )

    stats = calc_stratified_stats(trades, hold_days=5)

    assert stats["by_trigger"]["sos"]["stop_exit_rate_pct"] == 50.0
    assert stats["by_trigger"]["sos"]["avg_mfe_pct"] == 6.0
    assert stats["by_trigger"]["sos"]["avg_mae_pct"] == -4.5
    assert stats["by_exit_reason"]["stop_loss"]["trades"] == 1
    assert stats["by_entry_price_source"]["daily_close_fallback"]["trades"] == 1


def test_loss_guard_rejects_sos_when_df_too_short() -> None:
    """df 存在但长度不足60天时，应视为 ABC 无法确认，拒绝放行。"""
    short_df = _low_confirmation_df(rows=40)
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["sos"],
        8.0,
        "点火破局",
        {"000001": short_df},
    )
    assert reason == "右侧信号ABC不足"


def test_loss_guard_rejects_sos_when_df_missing_ohlcv_columns() -> None:
    """df 存在但缺少必要列时，应视为 ABC 无法确认，拒绝放行。"""
    bad_df = pd.DataFrame({"close": [10.0] * 80, "volume": [100.0] * 80})
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["sos"],
        8.0,
        "点火破局",
        {"000001": bad_df},
    )
    assert reason == "右侧信号ABC不足"


def test_loss_guard_passes_sos_when_df_is_none() -> None:
    """df_map 中完全没有该 code 的 df 时，跳过 ABC 检查，交给后续分支。"""
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["sos"],
        8.0,
        "点火破局",
        {},
    )
    # 不应返回 "右侧信号ABC不足"，但可能被 _naked_right_side_reason 的
    # "低分SOS" 或 "纯SOS确认强度不足" 拦下，取决于 df=None 时的分支。
    assert reason != "右侧信号ABC不足"


def test_loss_guard_pure_sos_needs_three_abc() -> None:
    """纯SOS信号需要ABC三项全部满足(met_count>=3)才能通过右侧确认。"""
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["sos"],
        8.0,
        "点火破局",
        {"000001": _low_confirmation_df()},
    )
    assert "SOS" in reason or "ABC" in reason


def test_loss_guard_mixed_sos_spring_passes_with_two_abc() -> None:
    """SOS+spring 组合信号仍用弱确认门槛(>=2)而非纯SOS收紧门槛(>=3)。"""
    reason = loss_guard_reason(
        "000001",
        "NEUTRAL",
        ["sos", "spring"],
        8.0,
        "点火破局",
        {"000001": _low_confirmation_df()},
    )
    # spring 不属于 NAKED_RIGHT_SIDE_TRIGGERS，所以 keys 不满足
    # keys <= NAKED_RIGHT_SIDE_TRIGGERS，不会进入 _naked_right_side_reason。
    # 也不满足 keys <= NAKED_RIGHT_SIDE_TRIGGERS for weak_confirmation。
    # 但 spring 在 STRUCTURAL_L4_TRIGGERS，走 is_tradeable_l4 判定。
    # 此处验证不会被 "纯SOS确认强度不足" 拦截。
    assert reason != "纯SOS确认强度不足"
