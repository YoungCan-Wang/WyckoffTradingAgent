from __future__ import annotations

import pandas as pd

from integrations.recommendation_performance import (
    build_market_performance_updates,
    build_us_performance_updates,
    group_records_by_market_code,
    latest_market_records,
    refresh_tracking_performance,
    refresh_us_tracking_performance,
)


def test_build_us_performance_updates_uses_entry_trade_date_window():
    hist = pd.DataFrame(
        {
            "date": ["2026-05-15", "2026-05-18"],
            "high": [10.8, 12.0],
            "low": [9.8, 9.0],
            "close": [10.0, 11.0],
        }
    )
    grouped = {"ABC.US": [{"id": 1, "code": "ABC.US", "recommend_date": 20260516, "initial_price": 10.0}]}

    updates, codes_no_data, latest_td = build_us_performance_updates(grouped, {"ABC.US": hist}, "now")

    assert codes_no_data == 0
    assert latest_td == "20260518"
    assert updates == [
        {
            "id": 1,
            "code": "ABC.US",
            "recommend_date": 20260516,
            "initial_price": 10.0,
            "current_price": 11.0,
            "change_pct": 10.0,
            "mfe_pct": 20.0,
            "mae_pct": -10.0,
            "range_amp_pct": 33.33,
            "mfe_price": 12.0,
            "mae_price": 9.0,
            "mfe_date": 20260518,
            "mae_date": 20260518,
            "performance_days": 2,
            "performance_updated_at": "now",
            "updated_at": "now",
        }
    ]


def test_build_us_performance_updates_reprices_stale_initial_price():
    hist = pd.DataFrame(
        {
            "date": ["2026-05-15", "2026-05-18"],
            "high": [26.0, 31.0],
            "low": [24.0, 29.0],
            "close": [25.0, 30.0],
        }
    )
    grouped = {"SPLT.US": [{"id": 1, "code": "SPLT.US", "recommend_date": 20260515, "initial_price": 100.0}]}

    updates, _, _ = build_us_performance_updates(grouped, {"SPLT.US": hist}, "now")

    assert updates[0]["initial_price"] == 25.0
    assert updates[0]["current_price"] == 30.0
    assert updates[0]["change_pct"] == 20.0
    assert updates[0]["mfe_pct"] == 24.0


def test_refresh_us_tracking_performance_fetches_forward_adjusted_hist(monkeypatch):
    captured: dict[str, object] = {}

    class FakeTickFlowClient:
        def __init__(self, api_key):
            assert api_key == "key"

        def get_klines_batch(self, symbols, *, period, count, adjust):
            captured["adjust"] = adjust
            assert symbols == ["SPLT.US"]
            assert period == "1d"
            assert count == 5
            return {"SPLT.US": pd.DataFrame({"date": ["2026-05-15"], "high": [26.0], "low": [24.0], "close": [25.0]})}

    monkeypatch.setenv("TICKFLOW_API_KEY", "key")
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr("integrations.recommendation_performance.is_admin_configured", lambda: True)
    monkeypatch.setattr("integrations.recommendation_performance.create_admin_client", lambda: object())
    monkeypatch.setattr(
        "integrations.recommendation_performance.fetch_records_from_table",
        lambda *_args: [{"id": 1, "code": "SPLT.US", "recommend_date": 20260515, "initial_price": 100.0}],
    )
    monkeypatch.setattr("integrations.recommendation_performance.upsert_to_table", lambda *_args: 1)
    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", FakeTickFlowClient)

    summary = refresh_us_tracking_performance(max_dates=1, kline_count=5)

    assert captured["adjust"] == "forward"
    assert summary["rows_updated"] == 1


def test_build_market_performance_updates_keeps_cn_code_numeric():
    hist = pd.DataFrame(
        {
            "date": ["2026-05-15"],
            "high": [11.0],
            "low": [9.5],
            "close": [10.5],
        }
    )
    grouped = {"600519": [{"id": 1, "code": 600519, "recommend_date": 20260515, "initial_price": 10.0}]}

    updates, codes_no_data, latest_td = build_market_performance_updates(grouped, {"600519": hist}, "now", "cn")

    assert codes_no_data == 0
    assert latest_td == "20260515"
    assert updates[0]["code"] == 600519
    assert updates[0]["change_pct"] == 0.0
    assert updates[0]["mfe_pct"] == 4.76


def test_refresh_tracking_performance_normalizes_cn_symbols(monkeypatch):
    captured: dict[str, object] = {}

    class FakeTickFlowClient:
        def __init__(self, api_key):
            assert api_key == "key"

        def get_klines_batch(self, symbols, *, period, count, adjust):
            captured["symbols"] = symbols
            captured["adjust"] = adjust
            assert period == "1d"
            assert count == 5
            return {"600519.SH": pd.DataFrame({"date": ["2026-05-15"], "high": [11.0], "low": [9.0], "close": [10.0]})}

    monkeypatch.setenv("TICKFLOW_API_KEY", "key")
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr("integrations.recommendation_performance.is_admin_configured", lambda: True)
    monkeypatch.setattr("integrations.recommendation_performance.create_admin_client", lambda: object())
    monkeypatch.setattr(
        "integrations.recommendation_performance.fetch_records_from_table",
        lambda *_args: [{"id": 1, "code": 600519, "recommend_date": 20260515, "initial_price": 10.0}],
    )
    monkeypatch.setattr("integrations.recommendation_performance.upsert_to_table", lambda *_args: 1)
    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", FakeTickFlowClient)
    monkeypatch.setattr("integrations.tickflow_client.normalize_cn_symbol", lambda code: f"{code}.SH")

    summary = refresh_tracking_performance("cn", max_dates=1, kline_count=5)

    assert captured == {"symbols": ["600519.SH"], "adjust": "forward"}
    assert summary["rows_updated"] == 1


def test_group_records_by_market_code_handles_cn_padding_and_global_symbols():
    assert group_records_by_market_code([{"code": 1}], "cn") == {"000001": [{"code": 1}]}
    assert group_records_by_market_code([{"code": "AAPL.US"}], "us") == {"AAPL.US": [{"code": "AAPL.US"}]}


def test_latest_market_records_keeps_latest_recommend_dates():
    rows = [
        {"code": "A.US", "recommend_date": 20260510},
        {"code": "B.US", "recommend_date": 20260512},
        {"code": "C.US", "recommend_date": 20260512},
        {"code": "D.US", "recommend_date": 20260513},
    ]

    assert latest_market_records(rows, 2) == rows[1:]
