"""Pure portfolio mark-to-market calculations in CNY."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.portfolio_symbol import normalize_portfolio_code


class PortfolioValuationError(ValueError):
    """Raised when a complete mark-to-market valuation is unavailable."""


@dataclass(frozen=True)
class PortfolioValuation:
    total_equity: float
    positions_value: float


def portfolio_currency(code: str) -> str:
    normalized = normalize_portfolio_code(code)
    if normalized.endswith(".HK"):
        return "HKD"
    if normalized.endswith(".US"):
        return "USD"
    return "CNY"


def calculate_portfolio_valuation(
    free_cash: float,
    positions: list[dict[str, Any]],
    prices: dict[str, float],
    cny_rates: dict[str, float],
) -> PortfolioValuation:
    positions_value = 0.0
    missing: list[str] = []
    for row in positions:
        code = normalize_portfolio_code(str(row.get("code", "") or ""))
        shares = int(row.get("shares", 0) or 0)
        if not code or shares <= 0:
            continue
        price = float(prices.get(code, 0.0) or 0.0)
        rate = float(cny_rates.get(portfolio_currency(code), 0.0) or 0.0)
        if price <= 0 or rate <= 0:
            missing.append(code)
            continue
        positions_value += shares * price * rate
    if missing:
        raise PortfolioValuationError(f"缺少完整行情或汇率: {', '.join(sorted(set(missing)))}")
    positions_value = round(positions_value, 2)
    return PortfolioValuation(
        total_equity=round(float(free_cash) + positions_value, 2),
        positions_value=positions_value,
    )
