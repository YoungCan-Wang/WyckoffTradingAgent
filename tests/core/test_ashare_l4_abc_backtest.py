from __future__ import annotations

from datetime import date

import pandas as pd

from core.backtest_replay import apply_abc_filter


def _hist(met_count: int) -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "date": date(2026, 1, 1),
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 1000,
            }
        ]
    )
    df.attrs["met_count"] = met_count
    return df


def test_abc_filter_keeps_codes_with_two_or_more_conditions(monkeypatch):
    def fake_score(df: pd.DataFrame, _sig_type: str) -> dict:
        return {"met_count": int(df.attrs.get("met_count", 0))}

    monkeypatch.setattr("core.backtest_replay.score_springboard_abc", fake_score)

    passed = apply_abc_filter(
        ["A", "B", "C", "D"],
        {"A": _hist(2), "B": _hist(1), "C": _hist(3), "D": pd.DataFrame()},
        {"sos": [("A", 1.0), ("B", 1.0), ("C", 1.0), ("D", 1.0)]},
    )

    assert passed == ["A", "C"]


def test_abc_filter_uses_best_count_across_trigger_types(monkeypatch):
    def fake_score(_df: pd.DataFrame, sig_type: str) -> dict:
        return {"met_count": 2 if sig_type == "spring" else 1}

    monkeypatch.setattr("core.backtest_replay.score_springboard_abc", fake_score)

    passed = apply_abc_filter(
        ["A", "B"],
        {"A": _hist(1), "B": _hist(1)},
        {"sos": [("A", 1.0), ("B", 1.0)], "spring": [("A", 1.0)]},
    )

    assert passed == ["A"]
