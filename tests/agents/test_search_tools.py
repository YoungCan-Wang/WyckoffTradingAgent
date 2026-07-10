from __future__ import annotations

import sys
from types import ModuleType

from agents import search_tools


def test_search_stock_enriches_a_share_result(monkeypatch) -> None:
    fetch_mod = ModuleType("integrations.fetch_a_share_csv")
    fetch_mod.get_all_stocks = lambda: [{"code": "300750", "name": "宁德时代"}]
    spot_mod = ModuleType("integrations.spot_snapshot")
    spot_mod.fetch_stock_spot_snapshot = lambda code: {"close": 188.8, "pct_chg": 3.2}
    market_metadata_mod = ModuleType("integrations.market_metadata")
    market_metadata_mod.fetch_market_cap_map = lambda: {"300750": 1234.5}
    meta_mod = ModuleType("tools.market_universe_meta")
    meta_mod.search_market_meta = lambda keyword, limit: []

    monkeypatch.setitem(sys.modules, "integrations.fetch_a_share_csv", fetch_mod)
    monkeypatch.setitem(sys.modules, "integrations.spot_snapshot", spot_mod)
    monkeypatch.setitem(sys.modules, "integrations.market_metadata", market_metadata_mod)
    monkeypatch.setitem(sys.modules, "tools.market_universe_meta", meta_mod)
    monkeypatch.setattr(search_tools, "_fetch_news_with_timeout", lambda _code: ["新闻A"])

    result = search_tools.search_stock_by_name("宁德")

    assert result == [
        {
            "code": "300750",
            "name": "宁德时代",
            "price": 188.8,
            "pct_chg": 3.2,
            "market_cap_yi": 1234.5,
            "news": ["新闻A"],
        }
    ]


def test_search_stock_uses_market_meta_fallback(monkeypatch) -> None:
    fetch_mod = ModuleType("integrations.fetch_a_share_csv")
    fetch_mod.get_all_stocks = lambda: []
    meta_mod = ModuleType("tools.market_universe_meta")
    meta_mod.search_market_meta = lambda keyword, limit: [
        {
            "code": "AAPL.US",
            "symbol": "AAPL.US",
            "name": "Apple",
            "market": "us",
            "asset_type": "stock",
            "currency": "USD",
        }
    ]

    monkeypatch.setitem(sys.modules, "integrations.fetch_a_share_csv", fetch_mod)
    monkeypatch.setitem(sys.modules, "tools.market_universe_meta", meta_mod)

    result = search_tools.search_stock_by_name("Apple")

    assert result[0]["code"] == "AAPL.US"
    assert result[0]["market"] == "us"
