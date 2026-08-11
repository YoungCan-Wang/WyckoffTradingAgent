"""Latest quote and official FX inputs for portfolio valuation."""

from __future__ import annotations

import os
from typing import Any

import requests

from core.portfolio_symbol import normalize_portfolio_code
from core.portfolio_valuation import portfolio_currency
from integrations.recommendation_tracking_common import resolve_tickflow_quote_price
from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol

_ECB_RATES_URL = "https://api.frankfurter.dev/v2/rates"
_FX_TIMEOUT_SECONDS = 6


def load_portfolio_marks(
    positions: list[dict[str, Any]],
    tickflow_api_key: str,
) -> tuple[dict[str, float], dict[str, float]]:
    codes = _position_codes(positions)
    if not codes:
        return {}, {"CNY": 1.0}
    quotes = TickFlowClient(api_key=tickflow_api_key).get_quotes([normalize_cn_symbol(code) for code in codes])
    prices = {code: _quote_price(code, quotes) for code in codes}
    currencies = {portfolio_currency(code) for code in codes}
    return prices, load_cny_rates(currencies)


def load_cny_rates(currencies: set[str]) -> dict[str, float]:
    rates = {"CNY": 1.0}
    foreign = sorted(currency for currency in currencies if currency != "CNY")
    unresolved: list[str] = []
    for currency in foreign:
        override = _positive_env_float(f"PORTFOLIO_{currency}_CNY_RATE")
        if override is None:
            unresolved.append(currency)
        else:
            rates[currency] = override
    if unresolved:
        rates.update(_fetch_ecb_cny_rates(unresolved))
    return rates


def _position_codes(positions: list[dict[str, Any]]) -> list[str]:
    codes = {
        normalize_portfolio_code(str(row.get("code", "") or ""))
        for row in positions
        if int(row.get("shares", 0) or 0) > 0
    }
    return sorted(code for code in codes if code)


def _quote_price(code: str, quotes: dict[str, dict[str, Any]]) -> float:
    symbol = normalize_cn_symbol(code)
    return resolve_tickflow_quote_price(quotes.get(symbol) or quotes.get(code))


def _positive_env_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _fetch_ecb_cny_rates(currencies: list[str]) -> dict[str, float]:
    response = requests.get(
        _ECB_RATES_URL,
        params={"base": "CNY", "quotes": ",".join(currencies), "providers": "ECB"},
        timeout=_FX_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    rows = response.json()
    rates: dict[str, float] = {}
    for row in rows if isinstance(rows, list) else []:
        currency = str(row.get("quote", "") or "").upper()
        cny_to_foreign = float(row.get("rate", 0.0) or 0.0)
        if currency in currencies and cny_to_foreign > 0:
            rates[currency] = 1.0 / cny_to_foreign
    return rates
