"""Tests for core.hk_risk_filter (HK penny-stock / extreme-move classification)."""

from __future__ import annotations

from core.hk_risk_filter import (
    classify_hk_risk,
    describe_hk_risk,
    is_extreme_daily_move,
    is_illiquid,
    is_penny_stock,
    is_price_discontinuous,
)


class TestIsPennyStock:
    def test_below_floor_is_penny(self):
        assert is_penny_stock(0.5)

    def test_at_or_above_floor_not_penny(self):
        assert not is_penny_stock(1.0)
        assert not is_penny_stock(5.0)

    def test_zero_or_negative_not_penny(self):
        assert not is_penny_stock(0.0)
        assert not is_penny_stock(-1.0)


class TestIsIlliquid:
    def test_below_threshold_illiquid(self):
        assert is_illiquid(1_000_000.0)

    def test_above_threshold_not_illiquid(self):
        assert not is_illiquid(5_000_000.0)


class TestIsExtremeDailyMove:
    def test_within_range_not_extreme(self):
        assert not is_extreme_daily_move(12.0)
        assert not is_extreme_daily_move(-15.0)

    def test_beyond_range_is_extreme(self):
        assert is_extreme_daily_move(60.0)
        assert is_extreme_daily_move(-70.0)


class TestIsPriceDiscontinuous:
    def test_normal_day_not_discontinuous(self):
        assert not is_price_discontinuous(open_=10.1, close=9.9, prev_close=10.0)

    def test_reverse_split_like_jump_up(self):
        assert is_price_discontinuous(open_=35.0, close=34.0, prev_close=10.0)

    def test_forward_split_like_jump_down(self):
        assert is_price_discontinuous(open_=2.0, close=1.9, prev_close=10.0)

    def test_missing_prev_close_not_discontinuous(self):
        assert not is_price_discontinuous(open_=10.0, close=10.0, prev_close=0.0)


class TestClassifyHkRisk:
    def test_clean_symbol_not_blocked(self):
        flags = classify_hk_risk(
            close=20.0,
            open_=19.8,
            prev_close=19.5,
            pct_chg=2.5,
            avg_turnover_hkd=10_000_000.0,
        )
        assert not flags.blocked
        assert not flags.is_penny_stock
        assert not flags.is_illiquid
        assert not flags.is_extreme_move
        assert not flags.is_price_discontinuous

    def test_penny_stock_blocked(self):
        flags = classify_hk_risk(close=0.3, avg_turnover_hkd=10_000_000.0)
        assert flags.blocked
        assert flags.is_penny_stock

    def test_illiquid_blocked(self):
        flags = classify_hk_risk(close=20.0, avg_turnover_hkd=500_000.0)
        assert flags.blocked
        assert flags.is_illiquid

    def test_extreme_move_blocked(self):
        flags = classify_hk_risk(close=20.0, pct_chg=80.0, avg_turnover_hkd=10_000_000.0)
        assert flags.blocked
        assert flags.is_extreme_move

    def test_default_turnover_assumes_liquid(self):
        flags = classify_hk_risk(close=20.0)
        assert not flags.is_illiquid


class TestDescribeHkRisk:
    def test_none_returns_empty(self):
        assert describe_hk_risk(None) == ""

    def test_penny_stock_description(self):
        flags = classify_hk_risk(close=0.3, avg_turnover_hkd=10_000_000.0)
        desc = describe_hk_risk(flags)
        assert "仙股" in desc

    def test_multiple_reasons_joined(self):
        flags = classify_hk_risk(close=0.3, pct_chg=80.0, avg_turnover_hkd=500_000.0)
        desc = describe_hk_risk(flags)
        assert "仙股" in desc
        assert "流动性" in desc
        assert "合股" in desc

    def test_clean_symbol_empty_description(self):
        flags = classify_hk_risk(close=20.0, avg_turnover_hkd=10_000_000.0)
        assert describe_hk_risk(flags) == ""
