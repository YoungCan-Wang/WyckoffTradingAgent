from __future__ import annotations

from core.recommendation_event_metrics import build_horizon_event, summarize_horizon_events


def test_build_horizon_event_uses_future_days_only() -> None:
    event = build_horizon_event(
        {"id": 1, "code": 600519, "name": "贵州茅台", "recommend_date": 20260515, "initial_price": 10.0},
        {
            "20260515": {"high": 99.0, "low": 1.0, "close": 10.0},
            "20260516": {"high": 10.5, "low": 9.8, "close": 10.1},
            "20260517": {"high": 11.1, "low": 9.5, "close": 10.8},
            "20260518": {"high": 10.9, "low": 9.6, "close": 10.6},
            "20260519": {"high": 10.7, "low": 9.7, "close": 10.5},
            "20260520": {"high": 10.8, "low": 9.4, "close": 10.4},
        },
        horizon_days=5,
        target_pct=10.0,
    )

    assert event["label_ready"] is True
    assert event["observed_days"] == 5
    assert event["mfe_horizon_pct"] == 11.0
    assert event["mae_horizon_pct"] == -6.0
    assert event["mfe_horizon_date"] == 20260517
    assert event["first_hit_date"] == 20260517
    assert event["days_to_hit"] == 2
    assert event["hit_target"] is True


def test_build_horizon_event_marks_partial_window_unready() -> None:
    event = build_horizon_event(
        {"code": "AAPL.US", "recommend_date": 20260515},
        {
            "20260515": {"high": 10.5, "low": 9.5, "close": 10.0},
            "20260516": {"high": 12.0, "low": 9.7, "close": 11.0},
        },
        horizon_days=5,
        target_pct=10.0,
    )

    assert event["label_ready"] is False
    assert event["label_status"] == "partial_window"
    assert event["hit_target"] is True
    assert event["observed_days"] == 1


def test_summarize_horizon_events_uses_ready_rows_only() -> None:
    summary = summarize_horizon_events(
        [
            {
                "label_ready": True,
                "hit_target": True,
                "mfe_horizon_pct": 12.0,
                "mae_horizon_pct": -3.0,
                "close_return_horizon_pct": 5.0,
            },
            {
                "label_ready": True,
                "hit_target": False,
                "mfe_horizon_pct": 4.0,
                "mae_horizon_pct": -6.0,
                "close_return_horizon_pct": -2.0,
            },
            {
                "label_ready": False,
                "hit_target": True,
                "mfe_horizon_pct": 15.0,
                "mae_horizon_pct": -2.0,
                "close_return_horizon_pct": 20.0,
            },
        ]
    )

    assert summary["rows_total"] == 3
    assert summary["rows_ready"] == 2
    assert summary["hit_count"] == 1
    assert summary["hit_rate_pct"] == 50.0
    assert summary["close_win_count"] == 1
    assert summary["close_win_rate_pct"] == 50.0
    assert summary["avg_close_return_horizon_pct"] == 1.5
    assert summary["close_payoff_ratio"] == 2.5
    assert summary["avg_mfe_horizon_pct"] == 8.0
    assert summary["mfe_mae_ratio"] == 1.78
    assert summary["mae_le_neg5_rate_pct"] == 50.0
