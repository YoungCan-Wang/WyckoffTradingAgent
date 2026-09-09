from __future__ import annotations

import pytest

from core.recommendation_event_metrics import build_horizon_event, summarize_horizon_events
from core.trade_friction import round_trip_cost_pct


def test_build_horizon_event_uses_future_days_only() -> None:
    event = build_horizon_event(
        {
            "id": 1,
            "code": 600519,
            "name": "贵州茅台",
            "recommend_date": 20260515,
            "initial_price": 1.0,
            "created_at": "2026-05-15T12:00:00+00:00",
        },
        {
            "20260515": {"high": 99.0, "low": 1.0, "close": 10.0},
            "20260516": {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.1},
            "20260517": {"high": 11.1, "low": 9.5, "close": 10.8},
            "20260518": {"high": 10.9, "low": 9.6, "close": 10.6},
            "20260519": {"high": 10.7, "low": 9.7, "close": 10.5},
            "20260520": {"high": 10.8, "low": 9.4, "close": 10.4},
            "20260521": {"high": 10.8, "low": 9.6, "close": 10.4},
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
    assert event["days_to_hit"] == 1
    assert event["hit_target"] is True
    assert event["entry_date"] == 20260516
    assert event["entry_price"] == 10.0
    assert event["tracking_initial_price"] == 1.0
    assert event["net_close_return_horizon_pct"] == pytest.approx(4.0 - round_trip_cost_pct(code="600519"))
    assert event["execution_verified"] is False
    assert event["availability_verified"] is True


def test_build_horizon_event_marks_partial_window_unready() -> None:
    event = build_horizon_event(
        {"code": "AAPL.US", "recommend_date": 20260515, "created_at": "2026-05-15T21:00:00Z"},
        {
            "20260515": {"high": 10.5, "low": 9.5, "close": 10.0},
            "20260516": {"open": 10.0, "high": 12.0, "low": 9.7, "close": 11.0},
        },
        horizon_days=5,
        target_pct=10.0,
    )

    assert event["label_ready"] is False
    assert event["label_status"] == "partial_window"
    assert event["hit_target"] is True
    assert event["observed_days"] == 0


@pytest.mark.parametrize(
    ("candle", "created_at", "status"),
    [
        ({"open": 11.0}, "2026-05-15T12:00:00Z", "entry_limit_up"),
        ({"open": 10.0, "volume": 0}, "2026-05-15T12:00:00Z", "entry_suspended"),
        ({}, "2026-05-15T12:00:00Z", "missing_entry_open"),
        ({"open": 10.0}, "2026-05-16T01:30:00Z", "published_after_entry"),
        ({"open": 10.0}, "2026-05-16T02:00:00Z", "published_after_entry"),
        ({"open": 10.0}, None, "missing_publication_time"),
        ({"open": 10.0}, "not-a-date", "invalid_publication_time"),
        ({"open": 10.0}, "2026-05-15T20:00:00", "invalid_publication_time"),
    ],
)
def test_entry_unavailable_is_counted_not_repriced(candle, created_at, status) -> None:
    event = build_horizon_event(
        {"code": 600519, "recommend_date": 20260515, "initial_price": 1, "created_at": created_at},
        {
            "20260515": {"close": 10},
            "20260516": {"high": 11, "low": 10, "close": 11, **candle},
            "20260517": {"open": 10, "high": 12, "low": 10, "close": 12},
        },
        horizon_days=1,
    )
    assert event["label_ready"] is False
    assert event["label_status"] == status
    assert event["availability_verified"] is False
    assert "net_close_return_horizon_pct" not in event
    summary = summarize_horizon_events([event])
    assert summary["rows_total"] == summary["rows_unready"] == 1
    assert summary["status_counts"] == {status: 1}
    assert summary["net_return_rows"] == 0


def test_each_recommendation_uses_its_own_next_open_and_holding_window() -> None:
    candles = {f"202605{day}": {"open": day, "high": day + 1, "low": day - 1, "close": day} for day in range(15, 20)}
    first = build_horizon_event(
        {"code": "00700.HK", "recommend_date": 20260515, "initial_price": 1, "created_at": "2026-05-15T12:00:00Z"},
        candles,
        horizon_days=1,
    )
    second = build_horizon_event(
        {"code": "00700.HK", "recommend_date": 20260517, "initial_price": 1, "created_at": "2026-05-17T12:00:00Z"},
        candles,
        horizon_days=1,
    )
    assert (first["entry_price"], second["entry_price"]) == (16, 18)
    assert (first["window_end_date"], second["window_end_date"]) == (20260517, 20260519)
    assert first["round_trip_cost_pct"] == pytest.approx(round_trip_cost_pct(code="00700.HK"))


@pytest.mark.parametrize(("code", "hit"), [("600519", False), ("00700.HK", True), ("AAPL.US", True)])
def test_entry_day_target_touch_respects_market_sellability(code, hit) -> None:
    event = build_horizon_event(
        {"code": code, "recommend_date": 20260904, "created_at": "2026-09-04T21:00:00Z"},
        {
            "20260904": {"close": 10},
            "20260907": {"open": 10, "high": 11.2, "low": 9.8, "close": 10},
            "20260908": {"high": 10.2, "low": 9.8, "close": 10},
        },
        horizon_days=1,
    )
    assert event["label_ready"] is True
    assert event["price_touch_target"] is True
    assert event["hit_target"] is hit
    assert event["days_to_hit"] == (0 if hit else None)
    assert event["net_close_return_horizon_pct"] < 0


@pytest.mark.parametrize("bad_close", [0, float("nan"), float("inf"), 12])
def test_invalid_window_is_not_silently_skipped(bad_close) -> None:
    event = build_horizon_event(
        {"code": "600519", "recommend_date": 20260904, "created_at": "2026-09-04T12:00:00Z"},
        {
            "20260904": {"close": 10},
            "20260907": {"open": 10, "high": 10.2, "low": 9.8, "close": 10},
            "20260908": {"high": 10.2, "low": 9.8, "close": bad_close},
            "20260909": {"open": 10, "high": 12, "low": 10, "close": 12},
        },
        horizon_days=1,
    )
    assert event["label_status"] == "invalid_price_window"
    assert event["label_ready"] is False
    assert "net_close_return_horizon_pct" not in event


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


def test_summary_does_not_convert_invalid_net_returns_to_zero() -> None:
    summary = summarize_horizon_events(
        [{"label_ready": True, "net_close_return_horizon_pct": value} for value in (None, "bad", float("nan"), 1)]
    )
    assert summary["rows_ready"] == 4
    assert summary["net_return_rows"] == 1
    assert summary["avg_net_close_return_horizon_pct"] == 1
