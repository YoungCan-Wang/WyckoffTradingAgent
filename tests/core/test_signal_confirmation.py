from __future__ import annotations

import pandas as pd

from core.signal_confirmation import check_confirmation, run_confirmation_cycle


def test_sos_confirmation_requires_reclaiming_signal_close():
    snap = {"snap_low": 9.2, "snap_close": 10.0, "snap_volume": 1_000_000}

    status, reason = check_confirmation(
        "sos",
        snap,
        {"low": 9.4, "close": 9.8, "volume": 700_000},
        days_elapsed=1,
    )

    assert status == "pending"
    assert "等待缩量" in reason


def test_sos_confirmation_accepts_shrinkage_above_signal_close():
    snap = {"snap_low": 9.2, "snap_close": 10.0, "snap_volume": 1_000_000}

    status, reason = check_confirmation(
        "sos",
        snap,
        {"low": 9.4, "close": 10.05, "volume": 700_000},
        days_elapsed=1,
    )

    assert status == "confirmed"
    assert "信号日收盘" in reason


def test_sos_confirmation_rejects_close_below_ma20():
    snap = {"snap_low": 9.2, "snap_close": 10.0, "snap_volume": 1_000_000}

    status, reason = check_confirmation(
        "sos",
        snap,
        {"low": 9.4, "close": 10.05, "volume": 700_000, "ma20": 10.5},
        days_elapsed=1,
    )

    assert status == "pending"
    assert "站稳MA20" in reason


def test_evr_confirmation_rejects_close_below_ma20():
    snap = {"snap_support": 9.5, "snap_close": 9.8}

    status, reason = check_confirmation(
        "evr",
        snap,
        {"low": 9.4, "close": 9.6, "volume": 500_000, "ma20": 10.2},
        days_elapsed=1,
    )

    assert status == "pending"
    assert "站稳MA20" in reason


def test_confirmation_cycle_marks_confirmed_source_for_step3():
    df = pd.DataFrame(
        [
            {"date": "2026-06-11", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000},
            {"date": "2026-06-12", "open": 10.3, "high": 10.8, "low": 10.1, "close": 10.7, "volume": 900},
        ]
    )

    updates, confirmed = run_confirmation_cycle(
        [
            {
                "id": 1,
                "code": 1,
                "name": "平安银行",
                "signal_type": "evr",
                "signal_date": "2026-06-11",
                "signal_score": 1.2,
                "days_elapsed": 0,
                "snap_support": 10.0,
                "snap_close": 10.2,
            }
        ],
        {"000001": df},
        "2026-06-12",
    )

    assert updates[0]["status"] == "confirmed"
    assert confirmed[0]["selection_source"] == "signal_confirmed"
    assert confirmed[0]["source_type"] == "signal_pending"
    assert confirmed[0]["confirm_date"] == "2026-06-12"
    assert confirmed[0]["confirm_reason"]
