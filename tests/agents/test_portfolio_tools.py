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


class TestPortfolioViewStopLoss:
    """view 模式必须带出 stop_loss：缺了会把有止损的持仓误报成无止损。"""

    def test_view_preserves_stop_loss(self) -> None:
        state = {
            "free_cash": 1000.0,
            "positions": [
                {"code": "600519", "name": "贵州茅台", "shares": 200, "cost": 1452.0, "stop_loss": 1380.5},
                {"code": "002270", "name": "法狮龙", "shares": 2500, "cost": 30.84, "stop_loss": None},
            ],
        }
        view = portfolio_tools._portfolio_view("pf-1", state)
        stops = {p["code"]: p["stop_loss"] for p in view["positions"]}
        assert stops == {"600519": 1380.5, "002270": None}

    def test_view_missing_stop_loss_count_is_accurate(self) -> None:
        state = {
            "free_cash": 0.0,
            "positions": [
                {"code": "600519", "name": "贵州茅台", "shares": 200, "cost": 1452.0, "stop_loss": 1380.5},
                {"code": "002270", "name": "法狮龙", "shares": 2500, "cost": 30.84},
            ],
        }
        view = portfolio_tools._portfolio_view("pf-1", state)
        missing = [p for p in view["positions"] if p.get("stop_loss") is None]
        assert len(missing) == 1
        assert missing[0]["code"] == "002270"
