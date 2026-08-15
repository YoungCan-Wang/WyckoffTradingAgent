"""成交回填的算账边界。"""

from __future__ import annotations

import pytest

from core.cash_portfolio import CashPortfolioConfig
from core.trade_fill import Fill, Holding, apply_fill

FREE = CashPortfolioConfig(commission_rate=0.0, min_commission=0.0, stamp_duty_rate=0.0, transfer_fee_rate=0.0)


def _hold(shares: int = 1000, cost: float = 10.0) -> Holding:
    return Holding(code="000001", name="平安银行", shares=shares, cost_price=cost, buy_dt="20260701")


def test_buy_into_empty_slot_sets_cost_and_spends_cash():
    result = apply_fill(None, 50_000.0, Fill("000001", "buy", 1000, 10.0, "20260710"), FREE)

    assert result.holding.shares == 1000
    assert result.holding.cost_price == pytest.approx(10.0)
    assert result.cash == pytest.approx(40_000.0)
    assert result.realized_pnl is None


def test_buy_more_averages_the_cost_and_refreshes_buy_date():
    result = apply_fill(_hold(), 50_000.0, Fill("000001", "buy", 1000, 12.0, "20260715"), FREE)

    assert result.holding.shares == 2000
    assert result.holding.cost_price == pytest.approx(11.0)
    # T+1 以最近一次买入为准，加仓当天整个仓位不可卖。
    assert result.holding.buy_dt == "20260715"


def test_cost_basis_absorbs_the_commission():
    cfg = CashPortfolioConfig(commission_rate=0.0003, min_commission=5.0, transfer_fee_rate=0.0, stamp_duty_rate=0.0)

    result = apply_fill(None, 50_000.0, Fill("000001", "buy", 1000, 10.0, "20260710"), cfg)

    assert result.fee == pytest.approx(5.0)
    assert result.holding.cost_price == pytest.approx(10.005)
    assert result.cash == pytest.approx(50_000.0 - 10_005.0)


def test_partial_sell_keeps_cost_and_reports_realised_pnl():
    result = apply_fill(_hold(), 1_000.0, Fill("000001", "sell", 400, 12.0, "20260720"), FREE)

    assert result.holding.shares == 600
    assert result.holding.cost_price == pytest.approx(10.0)
    assert result.realized_pnl == pytest.approx(800.0)
    assert result.cash == pytest.approx(1_000.0 + 4_800.0)


def test_full_sell_clears_the_position():
    result = apply_fill(_hold(), 0.0, Fill("000001", "sell", 1000, 9.0, "20260720"), FREE)

    assert result.holding is None
    assert result.realized_pnl == pytest.approx(-1_000.0)
    assert result.cash == pytest.approx(9_000.0)


def test_stamp_duty_only_applies_to_the_sell_side():
    cfg = CashPortfolioConfig(commission_rate=0.0, min_commission=0.0, transfer_fee_rate=0.0, stamp_duty_rate=0.0005)

    buy = apply_fill(None, 50_000.0, Fill("000001", "buy", 1000, 10.0, "20260710"), cfg)
    sell = apply_fill(_hold(), 0.0, Fill("000001", "sell", 1000, 10.0, "20260720"), cfg)

    assert buy.fee == pytest.approx(0.0)
    assert sell.fee == pytest.approx(5.0)


def test_selling_more_than_held_is_rejected():
    with pytest.raises(ValueError, match="超过持仓"):
        apply_fill(_hold(shares=500), 0.0, Fill("000001", "sell", 600, 10.0, "20260720"), FREE)


def test_selling_without_a_position_is_rejected():
    with pytest.raises(ValueError, match="无持仓可卖"):
        apply_fill(None, 0.0, Fill("000001", "sell", 100, 10.0, "20260720"), FREE)


def test_buying_beyond_available_cash_is_rejected():
    with pytest.raises(ValueError, match="现金不足"):
        apply_fill(None, 5_000.0, Fill("000001", "buy", 1000, 10.0, "20260710"), FREE)


def test_us_buy_converts_cash_to_cny_but_keeps_native_cost():
    """美元报价若直接扣人民币现金，10 股 @$200 只会少约 ¥2,000，真实应约 ¥14,000。"""
    result = apply_fill(
        None,
        50_000.0,
        Fill("AAPL.US", "buy", 10, 200.0, "20260815", name="Apple"),
        FREE,
        fx_to_cny=7.0,
    )

    assert result.holding is not None
    assert result.holding.cost_price == pytest.approx(200.0)
    assert result.cash == pytest.approx(50_000.0 - 14_000.0)
    assert result.fee == pytest.approx(0.0)


def test_us_sell_credits_cny_cash_and_reports_cny_pnl():
    holding = Holding(code="AAPL.US", name="Apple", shares=10, cost_price=180.0, buy_dt="20260101")
    result = apply_fill(holding, 1_000.0, Fill("AAPL.US", "sell", 5, 200.0, "20260815"), FREE, fx_to_cny=7.0)

    assert result.holding is not None
    assert result.holding.shares == 5
    assert result.cash == pytest.approx(1_000.0 + 7_000.0)
    assert result.realized_pnl == pytest.approx((200.0 - 180.0) * 5 * 7.0)


def test_missing_fx_rate_is_rejected():
    with pytest.raises(ValueError, match="汇率"):
        apply_fill(None, 50_000.0, Fill("AAPL.US", "buy", 1, 200.0, "20260815"), FREE, fx_to_cny=0.0)


@pytest.mark.parametrize(
    ("side", "shares", "price"),
    [("short", 100, 10.0), ("buy", 0, 10.0), ("buy", -100, 10.0), ("buy", 100, 0.0)],
)
def test_malformed_fills_are_rejected_at_construction(side, shares, price):
    with pytest.raises(ValueError):
        Fill("000001", side, shares, price, "20260710")
