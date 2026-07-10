from __future__ import annotations

from tools.market_universe_meta import load_symbol_name_map, search_market_meta
from workflows.market_universe_meta import etf_entries, market_entry


def test_market_entry_uses_market_currency() -> None:
    us = market_entry("AAPL.US", "us")
    hk = market_entry("00700.HK", "hk")

    assert us["code"] == "AAPL"
    assert us["currency"] == "USD"
    assert hk["code"] == "00700"
    assert hk["currency"] == "HKD"


def test_etf_entries_preserve_group_and_sector(tmp_path) -> None:
    path = tmp_path / "etf_cn.txt"
    path.write_text("# ---- 跨境 ETF ----\n513100 纳指100\n159920 恒生指数\n", encoding="utf-8")

    rows = etf_entries(path)

    assert rows[0]["symbol"] == "513100.SH"
    assert rows[0]["name"] == "纳指100ETF"
    assert rows[0]["group"] == "跨境 ETF"
    assert rows[1]["symbol"] == "159920.SZ"


def test_generated_meta_searches_us_hk_and_etf() -> None:
    assert search_market_meta("AAPL", limit=1)[0]["symbol"] == "AAPL.US"
    assert search_market_meta("00700", limit=1)[0]["symbol"] == "00700.HK"
    assert search_market_meta("纳指100", limit=1)[0]["code"] == "513100"


def test_generated_meta_searches_common_aliases() -> None:
    assert search_market_meta("苹果", limit=1)[0]["symbol"] == "AAPL.US"
    assert search_market_meta("腾讯", limit=1)[0]["symbol"] == "00700.HK"
    assert search_market_meta("英伟达", limit=1)[0]["symbol"] == "NVDA.US"
    assert [row["symbol"] for row in search_market_meta("苹果", limit=3)] == ["AAPL.US"]


def test_etf_name_map_loads_generated_meta() -> None:
    names = load_symbol_name_map(("etf_cn",))

    assert names["512880"] == "证券ETF"
    assert names["512880.SH"] == "证券ETF"
