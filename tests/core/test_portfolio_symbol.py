from __future__ import annotations

from core.portfolio_symbol import (
    is_cn_portfolio_code,
    is_supported_portfolio_code,
    normalize_portfolio_code,
    portfolio_lot_size,
    portfolio_name_conflict,
)


def test_normalize_portfolio_code_accepts_cn_hk_us() -> None:
    assert normalize_portfolio_code("601881") == "601881"
    assert normalize_portfolio_code("6881.HK") == "06881.HK"
    assert normalize_portfolio_code("06881.hk") == "06881.HK"
    assert normalize_portfolio_code("AAPL") == "AAPL.US"
    assert normalize_portfolio_code("aapl.us") == "AAPL.US"


def test_normalize_portfolio_code_rejects_ambiguous_bare_digits() -> None:
    assert normalize_portfolio_code("6881") == ""
    assert normalize_portfolio_code("60051") == ""
    assert normalize_portfolio_code("") == ""
    assert not is_supported_portfolio_code("6881")


def test_cn_helper_and_name_conflict() -> None:
    assert is_cn_portfolio_code("601881")
    assert not is_cn_portfolio_code("06881.HK")
    assert portfolio_name_conflict("06881.HK", "中国银河", "06881.HK") is None
    assert portfolio_name_conflict("601881", "中国银河", "中国银河") is None
    assert portfolio_name_conflict("601881", "错名", "中国银河") is not None


def test_portfolio_lot_size_cn_vs_hk_us() -> None:
    assert portfolio_lot_size("601881") == 100
    assert portfolio_lot_size("06881.HK") == 1
    assert portfolio_lot_size("AAPL.US") == 1
