from __future__ import annotations

import pytest

from core.portfolio_valuation import PortfolioValuationError, calculate_portfolio_valuation


def test_calculate_portfolio_valuation_converts_multi_market_positions() -> None:
    positions = [
        {"code": "600519", "shares": 100},
        {"code": "06881.HK", "shares": 1000},
        {"code": "AAPL.US", "shares": 10},
    ]

    result = calculate_portfolio_valuation(
        25_000,
        positions,
        {"600519": 1500, "06881.HK": 7.63, "AAPL.US": 200},
        {"CNY": 1, "HKD": 0.86, "USD": 7.1},
    )

    assert result.positions_value == 170_761.8
    assert result.total_equity == 195_761.8


def test_calculate_portfolio_valuation_rejects_partial_marks() -> None:
    with pytest.raises(PortfolioValuationError, match="06881.HK"):
        calculate_portfolio_valuation(
            10_000,
            [{"code": "600519", "shares": 100}, {"code": "06881.HK", "shares": 1000}],
            {"600519": 1500},
            {"CNY": 1, "HKD": 0.86},
        )
