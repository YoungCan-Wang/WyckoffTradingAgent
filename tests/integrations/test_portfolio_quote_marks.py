from __future__ import annotations

from integrations.portfolio_market_value import _quote_prev_close


def test_quote_prev_close_accepts_tickflow_aliases() -> None:
    quotes = {"600519": {"prev_close": 1490.0, "last_price": 1500.0}}
    assert _quote_prev_close("600519", quotes) == 1490.0
    assert _quote_prev_close("600519", {"600519.SH": {"pre_close": 1488.0}}) == 1488.0
    assert _quote_prev_close("600519", {"600519": {"last_price": 1500.0}}) == 0.0
