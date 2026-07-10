from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.backtest_execution import (
    ExitSimulationConfig,
    TradeRecord,
    build_daily_nav,
    calc_portfolio_metrics,
    resolve_trade_exit,
)


def test_build_daily_nav_uses_single_return_chain() -> None:
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    d3 = date(2026, 1, 7)
    record = TradeRecord(
        signal_date=d1,
        entry_date=d1,
        exit_date=d3,
        code="000001",
        name="平安银行",
        trigger="sos",
        score=1.0,
        entry_close=10.0,
        exit_close=12.0,
        ret_pct=20.0,
    )
    ohlc_cache = {"000001": {d1: (10, 10, 10, 10), d2: (10, 11, 10, 11), d3: (11, 12, 11, 12)}}

    nav = build_daily_nav([record], ohlc_cache, [d1, d2, d3], d1, d3)

    assert nav["positions_count"].tolist() == [1, 1, 1]
    assert nav["daily_ret_pct"].tolist() == pytest.approx([0.0, 10.0, 100 * (12 / 11 - 1)])
    assert nav["nav"].iloc[-1] == pytest.approx(1 + 0.1 + (12 / 11 - 1))


def test_calc_portfolio_metrics_empty_nav_has_stable_keys() -> None:
    metrics = calc_portfolio_metrics(build_daily_nav([], {}, [], date(2026, 1, 1), date(2026, 1, 2)))

    assert metrics["portfolio_trading_days"] == 0
    assert metrics["portfolio_avg_positions"] == 0.0
    assert metrics["portfolio_sharpe"] is None


def test_resolve_trade_exit_sltp_uses_threshold_price() -> None:
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    full_df = _daily_close_frame([(d1, 10.0), (d2, 11.0)])
    day_ohlc = {d1: (10.0, 10.0, 10.0, 10.0), d2: (10.0, 11.8, 9.8, 11.0)}

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=[d1, d2],
        actual_entry_idx=0,
        actual_exit_idx=1,
        actual_exit_anchor=d2,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(take_profit_pct=18.0),
    )

    assert exit_close == pytest.approx(11.8)
    assert exit_date == d2
    assert reason == "take_profit"


def test_resolve_trade_exit_sltp_zero_risk_controls_waits_for_time_exit() -> None:
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    full_df = _daily_close_frame([(d1, 10.0), (d2, 12.0)])
    day_ohlc = {d1: (10.0, 10.0, 10.0, 10.0), d2: (10.0, 30.0, 1.0, 12.0)}

    exit_close, exit_date, reason = resolve_trade_exit(
        full_df=full_df,
        day_ohlc=day_ohlc,
        trade_dates=[d1, d2],
        actual_entry_idx=0,
        actual_exit_idx=1,
        actual_exit_anchor=d2,
        signal_date=d1,
        entry_close=10.0,
        config=_exit_config(stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.0),
    )

    assert exit_close == pytest.approx(12.0)
    assert exit_date == d2
    assert reason == "time_exit"


def _exit_config(**overrides) -> ExitSimulationConfig:
    values = {
        "exit_mode": "sltp",
        "stop_loss_pct": -7.0,
        "take_profit_pct": 0.0,
        "trailing_stop_pct": 0.0,
        "trailing_activate_pct": 0.0,
        "sltp_priority": "stop_first",
        "atr_period": 14,
        "atr_multiplier": 2.0,
        "atr_hard_stop_pct": -9.0,
    }
    values.update(overrides)
    return ExitSimulationConfig(**values)


def _daily_close_frame(rows: list[tuple[date, float]]):
    return pd.DataFrame({"date": [row[0] for row in rows], "close": [row[1] for row in rows]})
