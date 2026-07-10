"""Harness tests for core.holding_diagnostic module."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from core.holding_diagnostic import (
    HoldingDiagnostic,
    diagnose_holdings,
    diagnose_one_stock,
    format_diagnostic_text,
)
from core.intraday_shakeout import PATH_DISTRIBUTION, PATH_WASHOUT
from tests.helpers.golden import assert_golden
from tests.helpers.synthetic_data import make_ohlcv


def _make_intraday_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(start=datetime(2026, 6, 22, 9, 30), periods=n, freq="1min", tz="Asia/Shanghai")
    close = pd.Series(closes)
    volume = pd.Series([1000.0] * n)
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )


def _drop_last_day_by_pct(df: pd.DataFrame, drop_pct: float) -> pd.DataFrame:
    """Force the last trading day to close drop_pct% below the prior close, keeping OHLC consistent."""
    out = df.copy()
    prev_close = float(out["close"].iloc[-2])
    new_close = prev_close * (1 + drop_pct / 100.0)
    out.loc[out.index[-1], "close"] = new_close
    out.loc[out.index[-1], "open"] = prev_close * 0.99
    out.loc[out.index[-1], "high"] = prev_close * 0.995
    out.loc[out.index[-1], "low"] = min(new_close, prev_close) * 0.98
    return out


class TestDiagnoseOneStock:
    def test_healthy_uptrend(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.008, seed=1)
        result = diagnose_one_stock("600519", "贵州茅台", cost=10.0, df=df)

        assert isinstance(result, HoldingDiagnostic)
        assert result.health == "🟢健康"
        assert result.ma_pattern in ("多头排列", "MA50>MA200(偏强)")
        assert result.pnl_pct > 0
        assert result.ma50 is not None
        assert result.ma200 is not None

    def test_danger_stop_loss_breached(self):
        df = make_ohlcv(n=250, trend="down", base=20.0, seed=2)
        latest = float(df["close"].iloc[-1])
        cost = latest * 1.15  # cost 15% above current → breached 7% stop
        result = diagnose_one_stock("000001", "平安银行", cost=cost, df=df)

        assert result.health == "🔴危险"
        assert result.stop_loss_status == "已穿止损"
        assert any("已穿" in r for r in result.health_reasons)

    def test_warning_signals(self):
        df = make_ohlcv(n=250, trend="down", base=15.0, seed=3)
        latest = float(df["close"].iloc[-1])
        cost = latest * 1.06  # moderate loss
        result = diagnose_one_stock("002230", "科大讯飞", cost=cost, df=df)

        assert result.health in ("🟡警戒", "🔴危险")
        assert len(result.health_reasons) > 0

    def test_take_profit_target_reached(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.008, seed=1)
        latest = float(df["close"].iloc[-1])
        cost = latest / 1.20  # pnl comfortably clears the +18% take-profit target
        result = diagnose_one_stock("600519", "贵州茅台", cost=cost, df=df)

        assert result.take_profit_status == "已达标"
        assert result.take_profit_18pct == cost * 1.18
        assert any("TP+18%" in r for r in result.health_reasons)

    def test_take_profit_not_reached_for_small_gain(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.008, seed=1)
        latest = float(df["close"].iloc[-1])
        cost = latest / 1.02  # small gain, well below +18% target
        result = diagnose_one_stock("600519", "贵州茅台", cost=cost, df=df)

        assert result.take_profit_status == "未达标"

    def test_short_dataframe_no_crash(self):
        df = make_ohlcv(n=10, trend="flat", base=12.0, seed=4)
        result = diagnose_one_stock("300750", "宁德时代", cost=12.0, df=df)

        assert isinstance(result, HoldingDiagnostic)
        assert result.ma50 is None
        assert result.ma200 is None
        assert result.ma_pattern == "数据不足"


class TestDiagnoseHoldings:
    def test_empty_dataframe_returns_danger(self):
        results = diagnose_holdings(
            holdings=[("600519", "贵州茅台", 1800.0)],
            df_map={"600519": pd.DataFrame()},
        )
        assert len(results) == 1
        assert results[0].health == "🔴危险"
        assert "无法获取行情数据" in results[0].health_reasons

    def test_missing_code_returns_danger(self):
        results = diagnose_holdings(
            holdings=[("999999", "不存在", 10.0)],
            df_map={},
        )
        assert len(results) == 1
        assert results[0].health == "🔴危险"


class TestExtremeDayIntradayPath:
    """当日跌幅显著时，接入分钟线应区分洗盘与出货，而非简单'跌了=走弱'。"""

    def _base_df(self, drop_pct: float) -> pd.DataFrame:
        df = make_ohlcv(n=250, trend="flat", base=10.0, volatility=0.01, seed=7)
        return _drop_last_day_by_pct(df, drop_pct)

    def test_washout_day_not_penalized_as_crash(self):
        df = self._base_df(drop_pct=-8.0)
        latest_close = float(df["close"].iloc[-1])
        support = float(df["close"].tail(20).min())
        # Intraday: dive below support early, recover to close near day high (washout signature).
        day_high = latest_close * 1.1
        first = [day_high - (day_high - support * 0.97) * i / 59 for i in range(60)]
        second = [support * 0.97 + (day_high * 0.98 - support * 0.97) * i / 59 for i in range(60)]
        intraday_df = _make_intraday_df(first + second)

        result = diagnose_one_stock("600519", "贵州茅台", cost=latest_close * 0.95, df=df, intraday_df=intraday_df)

        assert result.intraday_path == PATH_WASHOUT
        assert not any("暴跌" in r for r in result.health_reasons)
        assert any("跌幅不必等同走弱" in r for r in result.health_reasons)

    def test_distribution_day_flagged_as_risk(self):
        df = self._base_df(drop_pct=-8.0)
        latest_close = float(df["close"].iloc[-1])
        support = float(df["close"].tail(20).min())
        # Intraday: monotonic decline all day, closing well below support (distribution signature).
        day_high = latest_close * 1.1
        closes = [day_high - (day_high - support * 0.9) * i / 119 for i in range(120)]
        intraday_df = _make_intraday_df(closes)

        result = diagnose_one_stock("600519", "贵州茅台", cost=latest_close * 0.95, df=df, intraday_df=intraday_df)

        assert result.intraday_path == PATH_DISTRIBUTION
        assert any("出货" in r for r in result.health_reasons)
        assert result.health == "🔴危险"

    def test_no_intraday_df_skips_path_check(self):
        df = self._base_df(drop_pct=-8.0)
        latest_close = float(df["close"].iloc[-1])
        result = diagnose_one_stock("600519", "贵州茅台", cost=latest_close * 0.95, df=df, intraday_df=None)

        assert result.intraday_path == ""
        assert result.day_change_pct < 0

    def test_mild_day_change_skips_extreme_check(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.005, seed=1)
        result = diagnose_one_stock("600519", "贵州茅台", cost=10.0, df=df, intraday_df=_make_intraday_df([10.0] * 120))

        assert result.intraday_path == ""
        assert result.limit_move_desc == ""


class TestFormatDiagnosticText:
    def test_golden_healthy(self):
        df = make_ohlcv(n=250, trend="up", base=10.0, volatility=0.008, seed=1)
        d = diagnose_one_stock("600519", "贵州茅台", cost=10.0, df=df)
        text = format_diagnostic_text(d)
        assert_golden("diagnostic_healthy.txt", text)

    def test_golden_danger(self):
        df = make_ohlcv(n=250, trend="down", base=20.0, seed=2)
        latest = float(df["close"].iloc[-1])
        d = diagnose_one_stock("000001", "平安银行", cost=latest * 1.15, df=df)
        text = format_diagnostic_text(d)
        assert_golden("diagnostic_danger.txt", text)
