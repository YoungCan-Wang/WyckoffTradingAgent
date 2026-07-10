from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from workflows import backtest_intraday


class _FakeTickFlowClient:
    """Records get_klines calls so tests can assert on the endpoint/params used."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls: list[dict] = []

    def get_klines(self, symbol: str, **kwargs) -> pd.DataFrame:
        self.calls.append({"symbol": symbol, **kwargs})
        day = date(2026, 7, 3)
        return pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-07-03 14:55:00", tz="Asia/Shanghai")],
                "close": [10.29],
            }
        ).assign(date=str(day))


@pytest.fixture(autouse=True)
def _fake_tickflow_client(monkeypatch):
    fake_clients: list[_FakeTickFlowClient] = []

    def _factory(api_key: str) -> _FakeTickFlowClient:
        client = _FakeTickFlowClient(api_key)
        fake_clients.append(client)
        return client

    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", _factory)
    return fake_clients


def test_tickflow_entry_price_fetcher_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)

    assert backtest_intraday.tickflow_entry_price_fetcher_from_env() is None


def test_tickflow_entry_price_fetcher_queries_non_intraday_endpoint_for_history(monkeypatch, _fake_tickflow_client):
    monkeypatch.setenv("TICKFLOW_API_KEY", "tk_test")
    fetcher = backtest_intraday.tickflow_entry_price_fetcher_from_env()
    assert fetcher is not None

    price, source = fetcher("000001", date(2026, 7, 3), "14:55", {})

    assert price == pytest.approx(10.29)
    assert source == "tickflow_1m_14:55"
    client = _fake_tickflow_client[0]
    assert len(client.calls) == 1
    call = client.calls[0]
    # /v1/klines/intraday only serves the current session and ignores start/end time filters,
    # so historical entry-price lookups must go through the plain (non-intraday) endpoint,
    # which does honor start_time_ms/end_time_ms for any past trading day.
    assert call["intraday"] is False
    assert call["period"] == "1m"
    assert call["start_time_ms"] is not None
    assert call["end_time_ms"] is not None
