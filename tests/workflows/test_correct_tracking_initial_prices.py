from __future__ import annotations

from datetime import date

from integrations.recommendation_tracking_common import tracking_update_from_close_map
from workflows.recommendation_tracking_reprice import (
    _correct_initial_price_update,
    correct_tracking_initial_prices,
)


def test_tracking_update_uses_event_recommend_date_close():
    row = {"id": 2, "code": 1, "recommend_date": 20260518}
    update = tracking_update_from_close_map(
        row,
        1,
        ["20260516", "20260518"],
        {"20260516": 9.0, "20260518": 10.0},
        current_close=10.5,
        now_iso="now",
    )

    assert update is not None
    assert update["initial_price"] == 10.0
    assert update["current_price"] == 10.5
    assert update["change_pct"] == 5.0


def test_correct_initial_price_update_uses_event_date(monkeypatch):
    cache: dict = {}
    monkeypatch.setattr(
        "workflows.recommendation_tracking_reprice._resolve_initial_price_from_history",
        lambda code, day: 9.0 if day == date(2026, 5, 16) else 10.0,
    )

    update = _correct_initial_price_update(
        {
            "id": 7,
            "code": 1,
            "recommend_date": 20260518,
            "initial_price": 9.0,
            "current_price": 10.5,
            "change_pct": 16.67,
        },
        cache,
    )

    assert update is not None
    assert update["initial_price"] == 10.0
    assert update["change_pct"] == 5.0


def test_correct_initial_price_update_ignores_sub_cent_noise(monkeypatch):
    cache: dict = {}
    monkeypatch.setattr(
        "workflows.recommendation_tracking_reprice._resolve_initial_price_from_history",
        lambda code, day: 9.051433511,
    )

    update = _correct_initial_price_update(
        {
            "id": 8,
            "code": 1,
            "recommend_date": 20260518,
            "initial_price": 9.05,
            "current_price": 10.0,
            "change_pct": 10.5,
        },
        cache,
    )

    assert update is None


def test_correct_initial_price_update_skips_change_pct_only_drift(monkeypatch):
    cache: dict = {}
    monkeypatch.setattr(
        "workflows.recommendation_tracking_reprice._resolve_initial_price_from_history",
        lambda code, day: 13.8,
    )

    update = _correct_initial_price_update(
        {
            "id": 9,
            "code": 1,
            "recommend_date": 20260518,
            "initial_price": 13.8,
            "current_price": 14.86,
            "change_pct": 7.7,
        },
        cache,
    )

    assert update is None


def test_correct_tracking_initial_prices_dry_run_does_not_write(monkeypatch):
    records = [
        {
            "id": 1,
            "code": 1,
            "recommend_date": 20260516,
            "initial_price": 9.0,
            "current_price": 10.0,
            "change_pct": 11.11,
        },
        {
            "id": 2,
            "code": 1,
            "recommend_date": 20260518,
            "initial_price": 10.0,
            "current_price": 10.5,
            "change_pct": 5.0,
        },
    ]
    written: list[list] = []
    monkeypatch.setattr("workflows.recommendation_tracking_reprice.is_admin_configured", lambda: True)
    monkeypatch.setattr("workflows.recommendation_tracking_reprice.create_admin_client", lambda: object())
    monkeypatch.setattr(
        "workflows.recommendation_tracking_reprice.fetch_recommendation_tracking_records",
        lambda *_args, **_kwargs: records,
    )
    monkeypatch.setattr(
        "workflows.recommendation_tracking_reprice._resolve_initial_price_from_history",
        lambda code, day: 9.0 if day == date(2026, 5, 16) else 10.5,
    )
    monkeypatch.setattr(
        "workflows.recommendation_tracking_reprice.upsert_recommendation_tracking_price_updates",
        lambda _client, updates: written.append(updates) or len(updates),
    )

    summary = correct_tracking_initial_prices(apply=False)

    assert summary["rows_total"] == 2
    assert summary["rows_changed"] == 1
    assert summary["rows_written"] == 0
    assert written == []
