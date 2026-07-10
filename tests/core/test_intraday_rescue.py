from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from core.intraday_analysis import (
    _detect_platform_breakout,
    _detect_trend_establishment,
    _detect_vwap_reclaim,
    _score_rescue,
    _validate_volume_support,
    analyze_rescue_structure,
    ensure_intraday_df,
)


def _make_60m_df(
    bars: int = 80,
    start: float = 10.0,
    end: float = 10.5,
    volume: float = 10000.0,
    volume_tail_mult: float = 1.0,
) -> pd.DataFrame:
    idx = pd.date_range(start=datetime(2026, 5, 20, 9, 30), periods=bars, freq="60min", tz="Asia/Shanghai")
    close = np.linspace(start, end, bars)
    split = int(bars * 0.7)
    vol = np.full(bars, volume)
    vol[split:] = volume * volume_tail_mult
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": close * 0.999,
            "high": close * 1.003,
            "low": close * 0.997,
            "close": close,
            "volume": vol,
            "amount": close * vol,
        }
    )


class TestDetectPlatformBreakout:
    def test_breakout_detected(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0)
        df.loc[35:, "close"] = 10.2
        df.loc[35:, "high"] = 10.25
        result = ensure_intraday_df(df)
        is_break, strength = _detect_platform_breakout(result)
        assert is_break is True
        assert strength > 0

    def test_no_breakout(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0)
        result = ensure_intraday_df(df)
        is_break, _ = _detect_platform_breakout(result)
        assert is_break is False

    def test_insufficient_bars(self):
        df = _make_60m_df(bars=10, start=10.0, end=11.0)
        result = ensure_intraday_df(df)
        is_break, _ = _detect_platform_breakout(result)
        assert is_break is False


class TestDetectVwapReclaim:
    def test_reclaim_from_below(self):
        df = _make_60m_df(bars=40, start=9.5, end=10.5)
        result = ensure_intraday_df(df)
        is_reclaim, dist = _detect_vwap_reclaim(result)
        assert is_reclaim is True
        assert dist > 0

    def test_always_above_not_reclaim(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0)
        result = ensure_intraday_df(df)
        is_reclaim, _ = _detect_vwap_reclaim(result)
        assert is_reclaim is False


class TestDetectTrendEstablishment:
    def test_sustained_uptrend(self):
        df = _make_60m_df(bars=40, start=10.0, end=12.0)
        result = ensure_intraday_df(df)
        direction, slope = _detect_trend_establishment(result)
        assert direction == "up"
        assert slope > 0

    def test_flat_no_trend(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0)
        result = ensure_intraday_df(df)
        direction, _ = _detect_trend_establishment(result)
        assert direction == "flat"

    def test_spike_not_sustained(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0)
        df.loc[38, "close"] = 12.0
        result = ensure_intraday_df(df)
        direction, _ = _detect_trend_establishment(result)
        assert direction != "up" or direction == "flat"


class TestValidateVolumeSupport:
    def test_breakout_with_volume(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.5, volume=10000, volume_tail_mult=2.0)
        result = ensure_intraday_df(df)
        confirmed, ratio = _validate_volume_support(result, is_breakout=True)
        assert confirmed is True
        assert ratio >= 1.2

    def test_hollow_breakout(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.5, volume=10000, volume_tail_mult=0.5)
        result = ensure_intraday_df(df)
        confirmed, ratio = _validate_volume_support(result, is_breakout=True)
        assert confirmed is False
        assert ratio < 1.2


class TestScoreRescue:
    def test_full_signals(self):
        score, reasons = _score_rescue(
            breakout=True,
            breakout_strength=2.0,
            vwap_reclaim=True,
            vwap_dist=1.5,
            trend_dir="up",
            trend_slope=0.1,
            vol_confirmed=True,
            vol_ratio=1.8,
            vpc=0.4,
        )
        assert score >= 80
        assert len(reasons) >= 4

    def test_no_volume_caps_at_30(self):
        score, reasons = _score_rescue(
            breakout=True,
            breakout_strength=2.0,
            vwap_reclaim=True,
            vwap_dist=1.5,
            trend_dir="up",
            trend_slope=0.1,
            vol_confirmed=False,
            vol_ratio=0.8,
            vpc=0.4,
        )
        assert score <= 30
        assert any("封顶" in r for r in reasons)

    def test_partial_score(self):
        score, reasons = _score_rescue(
            breakout=False,
            breakout_strength=0.0,
            vwap_reclaim=True,
            vwap_dist=1.0,
            trend_dir="up",
            trend_slope=0.08,
            vol_confirmed=False,
            vol_ratio=1.0,
            vpc=0.3,
        )
        assert 40 <= score <= 70
        assert len(reasons) >= 2


class TestAnalyzeRescueStructure:
    def test_empty_df(self):
        result = analyze_rescue_structure(pd.DataFrame())
        assert result.rescue_score == 0.0
        assert result.bars_analyzed == 0

    def test_too_few_bars(self):
        df = _make_60m_df(bars=10, start=10.0, end=11.0)
        result = analyze_rescue_structure(df)
        assert result.rescue_score == 0.0

    def test_strong_structure(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0, volume=10000, volume_tail_mult=2.0)
        df.loc[30:, "close"] = 10.3
        df.loc[30:, "high"] = 10.35
        result = analyze_rescue_structure(df)
        assert result.rescue_score > 0
        assert result.bars_analyzed == 40

    def test_anti_fake_line_no_volume(self):
        df = _make_60m_df(bars=40, start=10.0, end=10.0, volume=10000, volume_tail_mult=0.3)
        df.loc[35:, "close"] = 10.3
        df.loc[35:, "high"] = 10.35
        result = analyze_rescue_structure(df)
        assert result.rescue_score <= 30

    def test_to_dict(self):
        df = _make_60m_df(bars=40, start=10.0, end=11.0)
        result = analyze_rescue_structure(df)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "rescue_score" in d
        assert "rescue_reasons" in d

    def test_with_30m_confirmation(self):
        df_60m = _make_60m_df(bars=40, start=10.0, end=12.0, volume=10000, volume_tail_mult=1.5)
        df_30m = _make_60m_df(bars=40, start=10.0, end=11.5)
        result_with = analyze_rescue_structure(df_60m, df_30m)
        result_without = analyze_rescue_structure(df_60m)
        assert result_with.rescue_score >= result_without.rescue_score
