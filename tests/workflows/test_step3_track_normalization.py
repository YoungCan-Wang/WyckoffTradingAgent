from __future__ import annotations

import pandas as pd

from workflows.step3_inputs import build_step3_track_inputs
from workflows.step3_selection import normalize_step3_candidates


def test_normalize_step3_candidates_accepts_track_aliases() -> None:
    candidates = normalize_step3_candidates(
        [
            {"code": "000001", "track": "accumulation"},
            {"code": "000002", "track": "trend"},
            {"code": "000003", "track": "lps"},
            {"code": "000004", "track": ""},
        ]
    )

    assert candidates["track"].tolist() == ["Accum", "Trend", "Accum", "Trend"]


def test_step3_track_inputs_normalize_track_aliases_when_grouping() -> None:
    selected_df = pd.DataFrame(
        [
            {"code": "000001", "name": "吸筹候选", "track": "lps"},
            {"code": "000002", "name": "趋势候选", "track": "trend_breakout"},
        ]
    )
    track_inputs = build_step3_track_inputs(
        selected_df,
        {"000001": _history_frame(), "000002": _history_frame()},
        [],
        {},
    )

    assert track_inputs.selected_codes_by_track["Accum"] == ["000001"]
    assert track_inputs.selected_codes_by_track["Trend"] == ["000002"]


def test_step3_track_inputs_surface_entry_quality_policy_tag() -> None:
    selected_df = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "吸筹候选",
                "track": "Trend",
                "entry_quality_tag": "入场质量A(75.0)",
                "entry_risk_flags": "缩量不足",
            }
        ]
    )

    track_inputs = build_step3_track_inputs(selected_df, {"000001": _history_frame()}, [], {})
    payload = track_inputs.payloads_by_track["Trend"][0]

    assert "入场质量A(75.0)" in payload
    assert "风险: 缩量不足" in payload


def _history_frame() -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=65, freq="B")
    close = [10.0 + idx * 0.01 for idx in range(len(dates))]
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [x * 1.01 for x in close],
            "low": [x * 0.99 for x in close],
            "close": close,
            "volume": [1_000_000.0] * len(dates),
        }
    )
