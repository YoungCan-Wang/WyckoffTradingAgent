from __future__ import annotations

import pandas as pd

from core.candidate_lanes import build_l1_candidate_lane_entries
from core.main_force_signal import analyze_main_force_signal


def _institutional_entry_frame() -> pd.DataFrame:
    values = [10.0] * 80
    for idx in range(10):
        values.append(values[-1] * (1.025 if idx % 2 == 0 else 0.995))
    dates = pd.date_range("2025-01-01", periods=len(values), freq="B")
    amount = [100_000_000.0] * 80 + [180_000_000.0 if idx % 2 == 0 else 55_000_000.0 for idx in range(10)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [value * 0.985 for value in values],
            "high": [value * 1.01 for value in values],
            "low": [value * 0.98 for value in values],
            "close": values,
            "volume": [1000.0] * len(values),
            "amount": amount,
        }
    )


def test_main_force_signal_marks_demand_absorption() -> None:
    signal = analyze_main_force_signal(_institutional_entry_frame())

    assert signal.score >= 0.70
    assert "疑似资金进场" in signal.labels
    assert "主动需求增强" in signal.labels
    assert signal.metrics["demand_supply_ratio"] > 2.0


def test_l1_candidate_lane_can_promote_main_force_entry() -> None:
    entries = build_l1_candidate_lane_entries(
        l1_symbols=["000001"],
        df_map={"000001": _institutional_entry_frame()},
        sector_map={"000001": "算力"},
        top_sectors=[],
        l2_symbols=[],
        channel_map={},
    )

    assert entries[0]["entry_type"] == "main_force_entry"
    assert entries[0]["score"] >= 78.0
    assert entries[0]["metrics"]["main_force_score"] >= 0.70
