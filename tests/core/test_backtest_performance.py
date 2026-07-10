from __future__ import annotations

from datetime import date

import pandas as pd

from core.backtest_execution import TradeRecord
from core.backtest_performance import BacktestPerformanceConfig, enrich_backtest_summary
from core.cash_portfolio import CashPortfolioConfig


def _hist() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date(2026, 1, day) for day in range(1, 5)],
            "open": [10.0, 11.0, 12.0, 13.0],
            "high": [10.5, 11.5, 12.5, 13.5],
            "low": [9.5, 10.5, 11.5, 12.5],
            "close": [10.0, 11.0, 12.0, 13.0],
        }
    )


def _record() -> TradeRecord:
    return TradeRecord(
        signal_date=date(2026, 1, 1),
        entry_date=date(2026, 1, 2),
        exit_date=date(2026, 1, 3),
        code="000001",
        name="平安银行",
        trigger="sos",
        score=2.0,
        entry_close=11.0,
        exit_close=12.0,
        ret_pct=9.09,
        track="Trend",
        regime="NEUTRAL",
        exit_reason="time_exit",
    )


def _config(cash: bool = False) -> BacktestPerformanceConfig:
    return BacktestPerformanceConfig(
        hold_days=1,
        buy_friction_pct=0.0,
        metrics_engine="legacy",
        wbt_fee_rate=0.0,
        wbt_n_jobs=1,
        cash_portfolio=cash,
        cash_config_by_style=[
            CashPortfolioConfig(initial_cash=100_000, max_positions=1, portfolio_style="slot_equal_4")
        ]
        if cash
        else [],
    )


def test_enrich_backtest_summary_adds_trade_metrics() -> None:
    records = [_record()]
    trades_df = pd.DataFrame([records[0].__dict__])

    summary = enrich_backtest_summary(
        {"trades": 1, "cash_portfolio_enabled": False},
        trades_df=trades_df,
        records=records,
        all_df_map={"000001": _hist()},
        ohlc_lookup_cache={},
        trade_dates=[date(2026, 1, day) for day in range(1, 5)],
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 4),
        config=_config(),
    )

    assert summary["win_rate_pct"] == 100.0
    assert summary["max_consecutive_losses"] == 0
    assert summary["stratified"]["by_exit_reason"]["time_exit"]["trades"] == 1
    assert not summary["_nav_df"].empty


def test_enrich_backtest_summary_handles_empty_trades() -> None:
    summary = enrich_backtest_summary(
        {},
        trades_df=pd.DataFrame(),
        records=[],
        all_df_map={},
        ohlc_lookup_cache={},
        trade_dates=[],
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 4),
        config=_config(),
    )

    assert summary["win_rate_pct"] is None
    assert summary["portfolio_trading_days"] == 0
    assert summary["wbt_available"] is None


def test_enrich_backtest_summary_adds_cash_portfolio_outputs() -> None:
    records = [_record()]
    trades_df = pd.DataFrame([records[0].__dict__])
    trades_df["entry_kind"] = "confirmed"

    summary = enrich_backtest_summary(
        {"trades": 1, "cash_portfolio_enabled": True},
        trades_df=trades_df,
        records=records,
        all_df_map={"000001": _hist()},
        ohlc_lookup_cache={},
        trade_dates=[date(2026, 1, day) for day in range(1, 5)],
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 4),
        config=_config(cash=True),
    )

    assert summary["cash_portfolio_style"] == "slot_equal_4"
    assert summary["cash_portfolio_style_summaries"][0]["cash_portfolio_trades"] == 1
    assert "slot_equal_4" in summary["_cash_portfolio_trades_by_style"]
