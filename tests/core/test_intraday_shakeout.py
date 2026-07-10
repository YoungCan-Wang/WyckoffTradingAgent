"""Tests for core.intraday_shakeout (washout vs. distribution intraday path classification)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from core.intraday_shakeout import (
    PATH_DISTRIBUTION,
    PATH_INSUFFICIENT_DATA,
    PATH_NEUTRAL,
    PATH_STRONG,
    PATH_WASHOUT,
    classify_intraday_path,
    describe_intraday_path,
)


def _make_intraday_df(closes: list[float], lows: list[float] | None = None, volumes: list[float] | None = None):
    n = len(closes)
    idx = pd.date_range(start=datetime(2026, 6, 22, 9, 30), periods=n, freq="1min", tz="Asia/Shanghai")
    close = pd.Series(closes)
    low = pd.Series(lows) if lows is not None else close * 0.999
    high = close * 1.001
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(volumes) if volumes is not None else pd.Series([1000.0] * n)
    amount = close * volume
    return pd.DataFrame(
        {"datetime": idx, "open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount}
    )


class TestClassifyIntradayPath:
    def test_insufficient_bars_returns_insufficient(self):
        df = _make_intraday_df([10.0] * 10)
        result = classify_intraday_path(df)
        assert result.path_type == PATH_INSUFFICIENT_DATA

    def test_empty_df_returns_insufficient(self):
        result = classify_intraday_path(pd.DataFrame())
        assert result.path_type == PATH_INSUFFICIENT_DATA

    def test_washout_breach_then_recover_to_high(self):
        """开盘跳水跌破支撑，随后拉回并收在日内高位区 → 洗盘。"""
        # first 60 bars: dive from 10.0 to 8.5 (breaches support=9.0)
        first = [10.0 - (1.5 * i / 59) for i in range(60)]
        # last 60 bars: recover from 8.5 back up to 9.8, closing near day high
        second = [8.5 + (1.3 * i / 59) for i in range(60)]
        closes = first + second
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df, support_level=9.0)
        assert result.day_low_breached_support
        assert not result.close_below_support
        assert result.close_pos > 0.6
        assert result.path_type == PATH_WASHOUT

    def test_distribution_close_below_support(self):
        """跌破支撑且收盘仍在支撑下方 → 出货/确认破位。"""
        n = 120
        closes = [10.0 - (2.0 * i / (n - 1)) for i in range(n)]  # monotonic decline, closes well below support
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df, support_level=9.0)
        assert result.close_below_support
        assert result.path_type == PATH_DISTRIBUTION

    def test_strong_no_breach_high_close(self):
        """全天未跌破支撑，收盘位置强 → 强势。"""
        n = 120
        closes = [10.0 + (0.5 * i / (n - 1)) for i in range(n)]  # steady climb, never breaches
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df, support_level=8.0)
        assert not result.day_low_breached_support
        assert result.path_type == PATH_STRONG

    def test_neutral_fallback(self):
        """无明确洗盘/破位/强势特征时落入中性。"""
        n = 120
        closes = [10.0 + 0.05 * ((i % 5) - 2) for i in range(n)]  # choppy, flat, mid close position
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df, support_level=1.0, day_change_pct=-1.0)
        assert result.path_type in (PATH_NEUTRAL, PATH_STRONG)

    def test_auto_support_when_not_provided(self):
        n = 120
        closes = [10.0 - (2.0 * i / (n - 1)) for i in range(n)]
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df)
        assert result.support_level > 0

    def test_day_change_pct_fallback_to_distribution(self):
        """无支撑位可用但当日跌幅巨大且收盘位置低 → 兜底判为出货。"""
        n = 120
        closes = [10.0 - (2.0 * i / (n - 1)) for i in range(n)]
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df, support_level=0.0, day_change_pct=-9.0)
        assert result.path_type == PATH_DISTRIBUTION


class TestDescribeIntradayPath:
    def test_describe_insufficient(self):
        df = _make_intraday_df([10.0] * 10)
        result = classify_intraday_path(df)
        assert describe_intraday_path(result) == "数据不足"

    def test_describe_includes_reason(self):
        n = 120
        closes = [10.0 - (2.0 * i / (n - 1)) for i in range(n)]
        df = _make_intraday_df(closes)
        result = classify_intraday_path(df, support_level=9.0)
        desc = describe_intraday_path(result)
        assert "出货" in desc or "破位" in desc
