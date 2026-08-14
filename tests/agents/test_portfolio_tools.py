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
    """view 必须带出 stop_loss。

    模型只能看到工具返回的字段：漏掉它，「哪些持仓没设止损」就会全部答错，
    而且答得很自信 —— 这是风控数字，不能靠字段缺失去推断。
    """

    def test_view_includes_stop_loss(self) -> None:
        state = {
            "positions": [{"code": "002270", "name": "A", "shares": 100, "cost": 30.0, "stop_loss": 27.5}],
            "free_cash": 1000.0,
        }
        view = portfolio_tools._portfolio_view("pid", state)
        assert view["positions"][0]["stop_loss"] == 27.5

    def test_missing_stop_loss_stays_none_not_zero(self) -> None:
        """真的没设过要是 None；给成 0 会被读成「止损价 0 元」。"""
        state = {"positions": [{"code": "002270", "shares": 100, "cost": 30.0}], "free_cash": 0}
        view = portfolio_tools._portfolio_view("pid", state)
        assert view["positions"][0]["stop_loss"] is None

    def test_stop_loss_survives_the_tool_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """端到端走 portfolio(mode="view")：TUI 与 MCP 都经过这条路径。"""
        state = {
            "positions": [{"code": "002270", "shares": 100, "cost": 30.0, "stop_loss": 27.5}],
            "free_cash": 0,
        }
        monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx: "pid")
        monkeypatch.setattr(portfolio_tools, "_load_portfolio_state", lambda _pid, _ctx: state)

        result = portfolio_tools.portfolio(mode="view")

        assert result["positions"][0]["stop_loss"] == 27.5
