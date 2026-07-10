from __future__ import annotations

import pandas as pd
import pytest

from workflows import backtest_strategy_replay as replay


@pytest.mark.parametrize("market", ["hk", "us"])
def test_pullback_strategy_enters_at_target_when_low_touches(market) -> None:
    candles = pd.DataFrame(
        [
            {"date": pd.Timestamp("2026-01-02").date(), "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0},
            {"date": pd.Timestamp("2026-01-03").date(), "open": 9.2, "high": 9.5, "low": 8.9, "close": 9.3},
        ]
    )
    strategy = replay.StrategySpec("test", "测试", "", "pullback", 10.0, (1.2, 1.5), "open_after_3d", 3)

    entry = replay._entry(strategy, candles, 0, 10.0)

    assert entry == (1, 9.0)


@pytest.mark.parametrize("market,code", [("hk", "00700.HK"), ("us", "ABC.US")])
def test_replay_one_returns_trade_after_target_hit(market, code) -> None:
    candles = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-01-02").date(),
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "amount": 20_000_000.0,
                "pct_chg": 0.0,
            },
            {
                "date": pd.Timestamp("2026-01-03").date(),
                "open": 10.0,
                "high": 13.0,
                "low": 9.9,
                "close": 12.0,
                "amount": 25_000_000.0,
                "pct_chg": 20.0,
            },
            {
                "date": pd.Timestamp("2026-01-04").date(),
                "open": 12.0,
                "high": 16.0,
                "low": 11.0,
                "close": 15.0,
                "amount": 30_000_000.0,
                "pct_chg": 25.0,
            },
        ]
    )
    strategy = replay.StrategySpec("test", "测试", "", "open", 0.0, (1.2, 1.5), "open_after_3d", 2)
    row = {
        "signal_date": "2026-01-01",
        "entry_date": "2026-01-02",
        "entry_close": 10.0,
        "code": code,
        "name": "Test Co",
        "trigger": "SOS",
        "score": 80.0,
    }

    trade = replay._replay_one(row, {code: candles}, strategy, replay.MARKET_RULES[market])

    assert trade is not None
    assert trade.ret_pct == 35.0
    assert trade.exit_date == "2026-01-04"


def test_replay_one_blocked_by_penny_stock_risk_returns_none() -> None:
    candles = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-01-02").date(),
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "amount": 20_000_000.0,
                "pct_chg": 0.0,
            },
            {
                "date": pd.Timestamp("2026-01-03").date(),
                "open": 0.4,
                "high": 0.5,
                "low": 0.3,
                "close": 0.4,
                "amount": 20_000_000.0,
                "pct_chg": -96.0,
            },
            {
                "date": pd.Timestamp("2026-01-04").date(),
                "open": 0.4,
                "high": 0.5,
                "low": 0.35,
                "close": 0.45,
                "amount": 20_000_000.0,
                "pct_chg": 12.5,
            },
        ]
    )
    strategy = replay.StrategySpec("test", "测试", "", "open", 0.0, (1.2, 1.5), "open_after_3d", 2)
    row = {
        "signal_date": "2026-01-01",
        "entry_date": "2026-01-02",
        "entry_close": 10.0,
        "code": "08001.HK",
        "name": "Penny Co",
        "trigger": "SOS",
        "score": 80.0,
    }

    trade = replay._replay_one(row, {"08001.HK": candles}, strategy, replay.MARKET_RULES["hk"])

    assert trade is None


def test_hk_risk_blocked_falls_back_to_close_times_volume_when_amount_zero() -> None:
    """TickFlow 港股历史 K 线 amount 字段恒为 0，_hk_risk_blocked 必须回退为 close*volume
    计算日均成交额，否则所有交易日都会被误判为流动性不足（真实生产回归 bug）。"""
    candles = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(f"2026-01-{2 + i:02d}").date(),
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000_000.0,
                "amount": 0.0,
                "pct_chg": 0.0,
            }
            for i in range(25)
        ]
    )

    assert replay._hk_risk_blocked(candles, 21) is False


@pytest.mark.parametrize("market", ["hk", "us"])
def test_summary_reports_strategy_metrics(market) -> None:
    trades = [
        replay.ReplayTrade("2026-01-01", "2026-01-02", "2026-01-03", "A", "A", 10, 12, 20.0, "SOS", 10),
        replay.ReplayTrade("2026-01-02", "2026-01-03", "2026-01-04", "B", "B", 10, 9, -10.0, "EVR", 8),
    ]
    strategy = replay.STRATEGIES[0]

    summary = replay._summary(strategy, trades, {"key": "p", "label": "P", "start": "s", "end": "e"}, "2", market)

    assert summary["strategy_id"] == strategy.id
    assert summary["board"] == market
    assert summary["trades"] == 2
    assert summary["win_rate_pct"] == 50.0
    assert summary["portfolio_total_ret_pct"] == pytest.approx(8.0)
