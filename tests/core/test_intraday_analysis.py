from __future__ import annotations

from datetime import datetime

import pandas as pd

from core.intraday_analysis import (
    analyze_intraday,
    ensure_intraday_df,
    infer_session_vwap,
    strip_tail_auction_bars,
)


def _make_1m_df(bars: int = 180, start: float = 10.0, end: float = 10.5) -> pd.DataFrame:
    idx = pd.date_range(start=datetime(2026, 5, 27, 9, 30), periods=bars, freq="1min", tz="Asia/Shanghai")
    close = pd.Series([start + (end - start) * i / max(bars - 1, 1) for i in range(bars)])
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": close * 0.999,
            "high": close * 1.003,
            "low": close * 0.997,
            "close": close,
            "volume": [1000.0] * bars,
            "amount": close * 1000.0,
        }
    )


def _make_5m_df(bars: int = 36, start: float = 10.0, end: float = 10.5) -> pd.DataFrame:
    idx = pd.date_range(start=datetime(2026, 5, 27, 9, 30), periods=bars, freq="5min", tz="Asia/Shanghai")
    close = pd.Series([start + (end - start) * i / max(bars - 1, 1) for i in range(bars)])
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": close * 0.999,
            "high": close * 1.003,
            "low": close * 0.997,
            "close": close,
            "volume": [5000.0] * bars,
            "amount": close * 5000.0,
        }
    )


class TestEnsureIntradayDf:
    def test_empty_input(self):
        result = ensure_intraday_df(pd.DataFrame())
        assert result.empty

    def test_missing_datetime_col(self):
        df = pd.DataFrame({"close": [10.0, 10.1]})
        assert ensure_intraday_df(df).empty

    def test_timestamp_column_converted(self):
        now_ms = int(datetime(2026, 5, 27, 10, 0).timestamp() * 1000)
        df = pd.DataFrame(
            {
                "timestamp": [now_ms, now_ms + 60_000],
                "close": [10.0, 10.1],
            }
        )
        result = ensure_intraday_df(df)
        assert len(result) == 2
        assert "datetime" in result.columns

    def test_normal_df(self):
        df = _make_1m_df(bars=60)
        result = ensure_intraday_df(df)
        assert len(result) == 60

    def test_strips_tail_auction_bars(self):
        idx = pd.date_range(start=datetime(2026, 5, 27, 14, 55), periods=6, freq="1min", tz="Asia/Shanghai")
        df = pd.DataFrame(
            {
                "datetime": idx,
                "open": [10.0] * 6,
                "high": [10.0] * 6,
                "low": [10.0] * 6,
                "close": [10.0] * 6,
                "volume": [100.0] * 6,
                "amount": [1000.0] * 6,
            }
        )
        result = ensure_intraday_df(df)
        # 14:55, 14:56 保留；14:57/58/59, 15:00 属于集合竞价，应被剔除。
        assert list(result["datetime"].dt.strftime("%H:%M")) == ["14:55", "14:56"]

    def test_keeps_data_when_entirely_within_auction_window(self):
        idx = pd.date_range(start=datetime(2026, 5, 27, 14, 58), periods=2, freq="1min", tz="Asia/Shanghai")
        df = pd.DataFrame(
            {
                "datetime": idx,
                "open": [10.0] * 2,
                "high": [10.0] * 2,
                "low": [10.0] * 2,
                "close": [10.0] * 2,
                "volume": [100.0] * 2,
                "amount": [1000.0] * 2,
            }
        )
        result = ensure_intraday_df(df)
        assert len(result) == 2

    def test_normalizes_lot_volume_when_amount_implies_100x(self):
        df = _make_1m_df(bars=60, start=10.0, end=10.5)
        df["amount"] = df["close"] * df["volume"] * 100.0  # volume 实为"手"
        result = ensure_intraday_df(df)
        assert abs(float(result["volume"].iloc[0]) - 1000.0 * 100.0) < 1e-6

    def test_does_not_touch_volume_when_already_consistent(self):
        df = _make_1m_df(bars=60, start=10.0, end=10.5)  # amount = close*volume，已自洽
        result = ensure_intraday_df(df)
        assert abs(float(result["volume"].iloc[0]) - 1000.0) < 1e-6


class TestStripTailAuctionBars:
    def test_no_datetime_column_passthrough(self):
        df = pd.DataFrame({"close": [10.0]})
        assert strip_tail_auction_bars(df) is df

    def test_empty_df_passthrough(self):
        df = pd.DataFrame()
        assert strip_tail_auction_bars(df) is df

    def test_coarse_grain_bars_are_not_filtered(self):
        """60分钟等粗粒度K线本身横跨集合竞价窗口，不应被整根剔除（避免跨天数据被误伤）。"""
        idx = pd.date_range(start=datetime(2026, 5, 20, 9, 30), periods=10, freq="60min", tz="Asia/Shanghai")
        df = pd.DataFrame({"datetime": idx, "close": [10.0] * 10})
        result = strip_tail_auction_bars(df)
        assert len(result) == 10


class TestInferSessionVwap:
    def test_zero_volume(self):
        close = pd.Series([10.0, 10.1, 10.2])
        vwap, scale = infer_session_vwap(close, 0.0, 0.0)
        assert vwap == close.median()

    def test_normal_vwap(self):
        close = pd.Series([10.0] * 30)
        vwap, scale = infer_session_vwap(close, 100000.0, 1000000.0)
        assert abs(vwap - 10.0) < 0.5

    def test_uses_real_amount_directly_without_scale_guessing(self):
        # amount/volume 已是真实 VWAP，不应再按 10/100/1000 猜测换算比例。
        close = pd.Series([10.0] * 30)
        vwap, scale = infer_session_vwap(close, total_volume=1000.0, total_amount=10050.0)
        assert abs(vwap - 10.05) < 1e-6
        assert scale == 1.0

    def test_falls_back_to_median_when_amount_inconsistent(self):
        # amount 与价格明显不自洽（如单位错乱）时，退化为近似值而不是硬算出离谱的 VWAP。
        close = pd.Series([10.0] * 30)
        vwap, scale = infer_session_vwap(close, total_volume=1000.0, total_amount=1_000_000_000.0)
        assert vwap == close.median()
        assert scale == 1.0


class TestAnalyzeIntraday:
    def test_empty_df_returns_zero_profile(self):
        profile = analyze_intraday(pd.DataFrame())
        assert profile.bars == 0
        assert profile.strength_score == 0.0

    def test_too_few_bars(self):
        df = _make_1m_df(bars=5)
        profile = analyze_intraday(df)
        assert profile.bars == 5
        assert profile.strength_score == 0.0

    def test_uptrend_profile(self):
        df_1m = _make_1m_df(bars=180, start=10.0, end=11.0)
        df_5m = _make_5m_df(bars=36, start=10.0, end=11.0)
        profile = analyze_intraday(df_1m, df_5m)
        assert profile.bars == 180
        assert profile.trend_short == "up"
        assert profile.close_pos > 0.8
        assert profile.strength_score > 60

    def test_downtrend_profile(self):
        df_1m = _make_1m_df(bars=180, start=11.0, end=10.0)
        df_5m = _make_5m_df(bars=36, start=11.0, end=10.0)
        profile = analyze_intraday(df_1m, df_5m)
        assert profile.trend_short == "down"
        assert profile.close_pos < 0.2
        assert profile.strength_score < 40

    def test_flat_profile(self):
        df_1m = _make_1m_df(bars=180, start=10.0, end=10.0)
        profile = analyze_intraday(df_1m)
        assert profile.trend_short == "flat"

    def test_to_dict(self):
        df_1m = _make_1m_df(bars=60)
        profile = analyze_intraday(df_1m)
        d = profile.to_dict()
        assert isinstance(d, dict)
        assert "strength_score" in d
        assert "vwap_pos" in d

    def test_spring_quality_with_context(self):
        df_1m = _make_1m_df(bars=180, start=10.0, end=10.5)
        df_1m.loc[10:15, "low"] = 9.5
        df_1m.loc[10:15, "close"] = 9.6
        df_1m.loc[16:20, "close"] = 10.1
        context = {"support_level": 10.0}
        profile = analyze_intraday(df_1m, daily_context=context)
        assert profile.spring_quality is not None
        assert profile.spring_quality > 0

    def test_spring_quality_none_without_context(self):
        df_1m = _make_1m_df(bars=60)
        profile = analyze_intraday(df_1m)
        assert profile.spring_quality is None
