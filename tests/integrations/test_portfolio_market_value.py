from __future__ import annotations

from types import SimpleNamespace

import pytest

from integrations import portfolio_market_value as market_value


def test_load_cny_rates_inverts_ecb_cny_quotes(monkeypatch) -> None:
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: [
            {"base": "CNY", "quote": "HKD", "rate": 1.1627906977},
            {"base": "CNY", "quote": "USD", "rate": 0.14},
        ],
    )
    monkeypatch.delenv("PORTFOLIO_HKD_CNY_RATE", raising=False)
    monkeypatch.delenv("PORTFOLIO_USD_CNY_RATE", raising=False)
    monkeypatch.setattr(market_value.requests, "get", lambda *_args, **_kwargs: response)

    rates = market_value.load_cny_rates({"CNY", "HKD", "USD"})

    assert rates["CNY"] == 1
    assert rates["HKD"] == pytest.approx(0.86)
    assert rates["USD"] == pytest.approx(1 / 0.14)


def test_load_cny_rates_prefers_broker_override(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_HKD_CNY_RATE", "0.85")
    monkeypatch.setattr(
        market_value.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network should not be used")),
    )

    assert market_value.load_cny_rates({"HKD"}) == {"CNY": 1.0, "HKD": 0.85}
