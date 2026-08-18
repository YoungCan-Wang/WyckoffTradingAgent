"""Tests for per-market trade cost.

A 股 / 港股 / 美股的费率结构不同，共用一套会算错：
- A 股佣金有最低 5 元门槛；印花税 0.05% **只收卖出侧**；过户费双边。
- 港股印花税 0.1% **买卖双边都征**，是其主要成本项。
- 美股无印花税；卖出侧有 SEC 规费与按**股数**计的 FINRA TAF（单笔有上限）。
"""

from __future__ import annotations

import pytest

from core.market_trade_cost import (
    CN,
    HK,
    US,
    US_FEES,
    market_of,
    round_trip_cost_pct,
    single_side_cost,
)


class TestMarketOf:
    def test_detects_each_market(self):
        assert market_of("600519") == CN
        assert market_of("06881.HK") == HK
        assert market_of("AAPL.US") == US

    def test_case_and_whitespace_tolerant(self):
        assert market_of(" aapl.us ") == US
        assert market_of("06881.hk") == HK

    def test_unknown_defaults_to_cn(self):
        assert market_of("") == CN
        assert market_of(None) == CN


class TestPublishedRates:
    """费率与公开来源对齐，避免手抄漂移。"""

    def test_hk_misc_rate_equals_sum_of_three_levies(self):
        from core.market_trade_cost import HK_FEES

        # HKEX 通函：SFC 交易征费 0.0027% + AFRC 0.00015% + 交易所交易费 0.00565%，均 per side。
        assert HK_FEES.misc_rate_both_sides == pytest.approx(0.000027 + 0.0000015 + 0.0000565)

    def test_hk_stamp_duty_is_ten_bps(self):
        from core.market_trade_cost import HK_FEES

        assert HK_FEES.stamp_duty_rate == pytest.approx(0.001)
        assert HK_FEES.stamp_duty_both_sides is True

    def test_us_has_no_stamp_duty(self):
        from core.market_trade_cost import US_FEES

        assert US_FEES.stamp_duty_rate == 0.0


class TestStampDutySides:
    def test_a_share_stamp_duty_is_sell_only(self):
        buy = single_side_cost(20_000.0, side="buy", market=CN)
        sell = single_side_cost(20_000.0, side="sell", market=CN)
        # 差额即卖出侧印花税 0.05%。
        assert sell - buy == pytest.approx(20_000.0 * 0.0005)

    def test_hk_stamp_duty_is_both_sides(self):
        buy = single_side_cost(20_000.0, side="buy", market=HK)
        sell = single_side_cost(20_000.0, side="sell", market=HK)
        assert buy == pytest.approx(sell)
        # 双边各含 0.1% 印花税，故买入侧成本远高于 A 股买入侧。
        assert buy > single_side_cost(20_000.0, side="buy", market=CN) * 3

    def test_us_has_no_stamp_duty_on_buy(self):
        buy = single_side_cost(3_000.0, side="buy", market=US, shares=15)
        assert buy == pytest.approx(0.0)


class TestMinCommission:
    def test_a_share_min_commission_dominates_small_orders(self):
        assert round_trip_cost_pct(2_000.0, market=CN) > round_trip_cost_pct(200_000.0, market=CN)

    def test_small_order_hits_the_floor(self):
        # 2000 * 0.0002 = 0.4 元 < 5 元门槛。
        assert single_side_cost(2_000.0, side="buy", market=CN) >= 5.0


class TestUsPerShareFee:
    def test_taf_scales_with_shares(self):
        few = single_side_cost(10_000.0, side="sell", market=US, shares=10)
        many = single_side_cost(10_000.0, side="sell", market=US, shares=1_000)
        assert many > few

    def test_taf_is_capped(self):
        """TAF 按股数计但单笔有上限，百万股不能线性膨胀。"""
        huge = single_side_cost(1_000.0, side="sell", market=US, shares=1_000_000)
        sec_part = 1_000.0 * US_FEES.sell_only_rate
        assert huge - sec_part == pytest.approx(US_FEES.per_share_fee_cap)

    def test_buy_side_has_no_taf(self):
        assert single_side_cost(10_000.0, side="buy", market=US, shares=1_000) == pytest.approx(0.0)


class TestRoundTrip:
    def test_hk_costs_more_than_a_share(self):
        """回归：港股往返成本约为 A 股 3 倍以上，不能再被当成零。"""
        assert round_trip_cost_pct(100_000.0, market=HK) > round_trip_cost_pct(100_000.0, market=CN) * 3

    def test_slippage_is_two_sided(self):
        without = round_trip_cost_pct(20_000.0, market=CN)
        with_slip = round_trip_cost_pct(20_000.0, market=CN, slippage_bps_per_side=5.0)
        assert with_slip - without == pytest.approx(0.1)

    def test_zero_and_negative_amounts_are_safe(self):
        assert single_side_cost(0.0, side="buy", market=CN) == 0.0
        assert single_side_cost(-100.0, side="sell", market=HK) == 0.0

    def test_negative_slippage_clamped(self):
        assert round_trip_cost_pct(20_000.0, market=CN, slippage_bps_per_side=-5.0) == pytest.approx(
            round_trip_cost_pct(20_000.0, market=CN)
        )
