from __future__ import annotations

import sys
import types
from datetime import date

from agents import backtest_tools


def test_run_backtest_normalizes_params_and_returns_compact_summary(monkeypatch):
    calls: list[dict] = []
    fake_module = types.ModuleType("workflows.backtest")

    class FakeBacktestWorkflowRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_run_backtest_request(request):
        calls.append(request.__dict__)
        return None, {
            "trades": 12,
            "win_rate_pct": 58.3,
            "cash_portfolio_final_cash": 123456.0,
            "cash_portfolio_total_return_pct": 23.456,
            "cash_portfolio_max_drawdown_pct": -6.7,
            "cash_portfolio_trades": 9,
            "cash_portfolio_style": "confirmation_only",
        }

    fake_module.BacktestWorkflowRequest = FakeBacktestWorkflowRequest
    fake_module.run_backtest_request = fake_run_backtest_request
    monkeypatch.setitem(sys.modules, "workflows.backtest", fake_module)
    monkeypatch.setattr(backtest_tools, "ensure_tushare_token", lambda _ctx: None)

    result = backtest_tools.run_backtest(
        start="2026-01-01",
        end="2026-01-31",
        hold_days=99,
        top_n=-3,
        board=" all ",
        stop_loss_pct=5,
        take_profit_pct=-2,
    )

    assert calls[0]["start_dt"] == date(2026, 1, 1)
    assert calls[0]["end_dt"] == date(2026, 1, 31)
    assert calls[0]["hold_days"] == 60
    assert calls[0]["top_n"] == 0
    assert calls[0]["board"] == "all"
    assert calls[0]["stop_loss_pct"] == 0.0
    assert calls[0]["take_profit_pct"] == 0.0
    assert calls[0]["cash_portfolio"] is True
    assert calls[0]["portfolio_styles"] == "confirmation_only"
    assert calls[0]["entry_price_mode"] == "open"
    assert result["period"] == "2026-01-01 ~ 2026-01-31"
    assert result["cash_final"] == 123456.0
    assert result["cash_return_pct"] == 23.456
    assert result["entry_price_mode"] == "open"


def test_run_backtest_passes_close_entry_price_mode(monkeypatch):
    calls: list[dict] = []
    fake_module = types.ModuleType("workflows.backtest")

    class FakeBacktestWorkflowRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_run_backtest_request(request):
        calls.append(request.__dict__)
        return None, {}

    fake_module.BacktestWorkflowRequest = FakeBacktestWorkflowRequest
    fake_module.run_backtest_request = fake_run_backtest_request
    monkeypatch.setitem(sys.modules, "workflows.backtest", fake_module)
    monkeypatch.setattr(backtest_tools, "ensure_tushare_token", lambda _ctx: None)

    result = backtest_tools.run_backtest(entry_price_mode=" CLOSE ")

    assert calls[0]["entry_price_mode"] == "close"
    assert result["entry_price_mode"] == "close"


def test_run_backtest_rejects_unknown_entry_price_mode(monkeypatch):
    calls: list[dict] = []
    fake_module = types.ModuleType("workflows.backtest")

    class FakeBacktestWorkflowRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def fake_run_backtest_request(request):
        calls.append(request.__dict__)
        return None, {}

    fake_module.BacktestWorkflowRequest = FakeBacktestWorkflowRequest
    fake_module.run_backtest_request = fake_run_backtest_request
    monkeypatch.setitem(sys.modules, "workflows.backtest", fake_module)
    monkeypatch.setattr(backtest_tools, "ensure_tushare_token", lambda _ctx: None)

    result = backtest_tools.run_backtest(entry_price_mode="bogus")

    assert calls[0]["entry_price_mode"] == "open"
    assert result["entry_price_mode"] == "open"
