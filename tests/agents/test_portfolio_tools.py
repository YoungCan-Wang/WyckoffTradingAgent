"""Tests for agents.portfolio_tools extreme-day intraday fetch gating."""

from __future__ import annotations

import pandas as pd
import pytest

from agents import portfolio_tools


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


class TestFetchIntradayIfExtremeDay:
    def test_mild_day_change_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")
        df = _df([10.0, 9.8])  # -2%, below the -5% threshold
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is None

    def test_insufficient_history_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")
        df = _df([10.0])
        assert portfolio_tools._fetch_intraday_if_extreme_day("600519", df) is None

    def test_missing_api_key_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
        df = _df([10.0, 9.0])  # -10%, exceeds threshold
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is None

    def test_extreme_drop_with_api_key_fetches_intraday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")
        sentinel = pd.DataFrame({"close": [9.0]})

        class _FakeClient:
            def __init__(self, api_key: str) -> None:
                assert api_key == "dummy-key"

            def get_intraday(self, code: str, *, period: str, count: int) -> pd.DataFrame:
                assert code == "600519"
                return sentinel

        monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", _FakeClient)
        df = _df([10.0, 9.0])  # -10%, exceeds threshold
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is sentinel

    def test_client_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TICKFLOW_API_KEY", "dummy-key")

        class _RaisingClient:
            def __init__(self, api_key: str) -> None:
                pass

            def get_intraday(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", _RaisingClient)
        df = _df([10.0, 9.0])
        result = portfolio_tools._fetch_intraday_if_extreme_day("600519", df)
        assert result is None
