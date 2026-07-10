from __future__ import annotations

import pandas as pd

from core.candidate_lanes import build_l1_candidate_lane_entries, merge_candidate_entries


def _frame(values: list[float], *, volume_tail: float = 1000.0) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(values), freq="B")
    volume = [1000.0] * max(len(values) - 5, 0) + [volume_tail] * min(5, len(values))
    return pd.DataFrame(
        {
            "date": dates,
            "open": [v * 0.99 for v in values],
            "high": [v * 1.01 for v in values],
            "low": [v * 0.98 for v in values],
            "close": values,
            "volume": volume,
        }
    )


def test_l1_candidate_lanes_can_create_trend_breakout_without_l2() -> None:
    values = [10 + idx * 0.02 for idx in range(90)] + [12 + idx * 0.08 for idx in range(30)]

    entries = build_l1_candidate_lane_entries(
        l1_symbols=["000001"],
        df_map={"000001": _frame(values)},
        sector_map={"000001": "共封装光学(CPO)"},
        top_sectors=["共封装光学(CPO)"],
        l2_symbols=[],
        channel_map={},
    )

    assert entries[0]["code"] == "000001"
    assert entries[0]["entry_type"] == "trend_breakout"
    assert entries[0]["lane"] == "trend_breakout"
    assert entries[0]["metrics"]["ret20"] > 0
    assert entries[0]["metrics"]["dist_ma20"] > 0


def test_l1_candidate_lanes_block_overheated_chase() -> None:
    values = [10 + idx * 0.02 for idx in range(100)] + [13 + idx * 0.45 for idx in range(20)]

    entries = build_l1_candidate_lane_entries(
        l1_symbols=["000002"],
        df_map={"000002": _frame(values, volume_tail=2200)},
        sector_map={"000002": "存储芯片"},
        top_sectors=["存储芯片"],
        l2_symbols=[],
        channel_map={},
    )

    assert entries == []


def test_merge_candidate_entries_prefers_mainline_then_lane_priority() -> None:
    merged = merge_candidate_entries(
        [{"code": "000001", "entry_type": "sos", "score": 99.0}],
        [{"code": "000001", "entry_type": "trend_lane_pullback", "score": 76.0}],
        [{"code": "000002", "signal_key": "mainline", "entry_type": "主线回踩MA20", "score": 70.0}],
    )

    assert [item["entry_type"] for item in merged] == ["主线回踩MA20", "trend_lane_pullback"]


def test_merge_candidate_entries_normalizes_lane_key_aliases() -> None:
    merged = merge_candidate_entries(
        [{"code": "000001", "entry_type": "sos", "score": 99.0}],
        [{"code": "000001", "entry_type": "Trend-Lane-Pullback", "score": 76.0}],
    )

    assert [item["entry_type"] for item in merged] == ["Trend-Lane-Pullback"]


def test_merge_candidate_entries_treats_invalid_scores_as_zero() -> None:
    merged = merge_candidate_entries(
        [{"code": "000001", "entry_type": "spring", "score": float("nan")}],
        [{"code": "000001", "entry_type": "spring", "score": 80.0}],
        [{"code": "000002", "entry_type": "spring", "score": float("inf")}],
    )

    assert [(item["code"], item["score"]) for item in merged] == [("000001", 80.0), ("000002", 0.0)]
