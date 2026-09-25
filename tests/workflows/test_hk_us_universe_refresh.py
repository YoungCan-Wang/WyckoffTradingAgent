from __future__ import annotations

import pandas as pd
import pytest

from workflows.hk_us_universe_refresh import (
    MIN_HK_SYMBOLS,
    clean_listing_name,
    hk_pool,
    keep_us_listing,
    listing_names,
    require_pool_size,
    us_pool,
)


def test_keep_us_listing_rejects_derivatives_and_keeps_ordinary_names() -> None:
    assert keep_us_listing(
        {
            "Symbol": "BFAM",
            "Security Name": "Bright Horizons Family Solutions Inc. Common Stock",
            "ETF": "N",
            "Test Issue": "N",
        },
        "Symbol",
    )
    assert not keep_us_listing(
        {"Symbol": "SPY", "Security Name": "SPDR S&P 500 ETF", "ETF": "Y", "Test Issue": "N"},
        "Symbol",
    )
    assert not keep_us_listing(
        {"ACT Symbol": "BNY$K", "Security Name": "Depositary Shares Preferred Stock", "ETF": "N", "Test Issue": "N"},
        "ACT Symbol",
    )
    assert not keep_us_listing(
        {"Symbol": "ZZZ", "Security Name": "Example Warrant", "ETF": "N", "Test Issue": "N"},
        "Symbol",
    )


def test_listing_names_and_us_pool_prefer_tushare_name() -> None:
    rows = [
        {"Symbol": "AAPL", "Security Name": "Apple Inc. - Common Stock", "ETF": "N", "Test Issue": "N"},
        {"Symbol": "MSFT", "Security Name": "Microsoft Corporation Common Stock", "ETF": "N", "Test Issue": "N"},
    ]
    symbols, names = us_pool(listing_names(rows, "Symbol"), {"AAPL": "苹果"})

    assert symbols == ["AAPL.US", "MSFT.US"]
    assert names["AAPL.US"] == "苹果"
    assert names["MSFT.US"] == "Microsoft Corporation"
    assert clean_listing_name("Apple Inc. - Common Stock") == "Apple Inc."


def test_hk_pool_keeps_five_digit_listings() -> None:
    frame = pd.DataFrame(
        [
            {"ts_code": "07666.HK", "name": "剂泰科技-P"},
            {"ts_code": "700.HK", "name": "短代码"},
            {"ts_code": "00700.hk", "name": "腾讯控股"},
        ]
    )

    symbols, names = hk_pool(frame)

    assert symbols == ["00700.HK", "07666.HK"]
    assert names["07666.HK"] == "剂泰科技-P"


def test_require_pool_size_rejects_a_truncated_download() -> None:
    with pytest.raises(RuntimeError, match="低于"):
        require_pool_size("港股", ["00700.HK"], MIN_HK_SYMBOLS)
