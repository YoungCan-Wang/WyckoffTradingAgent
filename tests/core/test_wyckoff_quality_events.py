from __future__ import annotations

import pandas as pd

from core.signal_lifecycle import evaluate_signal_lifecycle
from core.wyckoff_events import classify_wyckoff_event


def test_classify_wyckoff_event_right_side_ignition():
    event = classify_wyckoff_event(
        ("sos",),
        stage="Markup",
        channel="点火破局+结构TR",
        score=12.5,
        regime="RISK_ON",
    )

    assert event.event_id == "right_side_ignition"
    assert event.label == "右侧点火"
    assert event.track == "Trend"
    assert event.confidence == "high"
    assert "阶段=Markup" in event.reasons
    assert "水温=RISK_ON" in event.reasons


def test_classify_wyckoff_event_core_branches():
    cases = [
        (("spring", "lps"), "", 6.0, "accumulation_repair_resonance", "Accum", "high"),
        (("spring",), "", 0.0, "spring_reclaim", "Accum", "medium"),
        (("lps",), "", 0.0, "lps_pullback_confirm", "Accum", "medium"),
        (("evr",), "Markup", 0.0, "volume_absorption", "Trend", "medium"),
        (("sos",), "", 0.0, "sos_watch", "Trend", "medium"),
        ((), "", 0.0, "wyckoff_watch", "Watch", "low"),
    ]

    for triggers, stage, score, event_id, track, confidence in cases:
        event = classify_wyckoff_event(triggers, stage=stage, score=score)

        assert event.event_id == event_id
        assert event.track == track
        assert event.confidence == confidence
        assert event.watch_points


def test_signal_lifecycle_marks_done_and_pending_horizons():
    df = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=6).astype(str),
            "close": [10, 11, 12, 11, 13, 14],
        }
    )

    lifecycle = evaluate_signal_lifecycle(df, code="000001", signal_date="2024-01-02", horizons=(1, 3, 10))

    assert lifecycle.code == "000001"
    assert lifecycle.entry_price == 11
    assert lifecycle.outcomes[0].status == "done"
    assert round(lifecycle.outcomes[0].return_pct, 2) == 9.09
    assert lifecycle.outcomes[-1].status == "pending"


def test_signal_lifecycle_uses_future_low_for_drawdown():
    df = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=3).astype(str),
            "close": [10, 11, 12],
            "low": [7, 8.5, 10.5],
        }
    )

    lifecycle = evaluate_signal_lifecycle(df, code="000001", signal_date="2024-01-01", horizons=(1,))

    assert round(lifecycle.outcomes[0].return_pct, 2) == 10.0
    assert round(lifecycle.outcomes[0].max_drawdown_pct, 2) == -15.0
