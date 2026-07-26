from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from core.backtest_execution import calc_trade_excursion_pct, entry_on_or_after, price_at_or_before


def test_price_at_or_before_uses_last_minute_before_target() -> None:
    day = datetime(2026, 1, 5).date()
    tz = ZoneInfo("Asia/Shanghai")
    df = pd.DataFrame(
        {
            "datetime": [
                datetime(2026, 1, 5, 14, 54, tzinfo=tz),
                datetime(2026, 1, 5, 14, 55, tzinfo=tz),
                datetime(2026, 1, 5, 14, 56, tzinfo=tz),
            ],
            "close": [10.1, 10.2, 10.3],
        }
    )

    assert price_at_or_before(df, day, "14:55") == 10.2


def test_close_mode_uses_next_day_closing_price() -> None:
    day = datetime(2026, 1, 5).date()
    df = pd.DataFrame({"date": [day], "open": [10.0], "high": [10.8], "low": [9.8], "close": [10.5]})

    price, entry_date, source = entry_on_or_after(
        df,
        "000001",
        day,
        mode="close",
        entry_time="",
        fallback="close",
        intraday_cache={},
    )

    assert price == 10.5
    assert entry_date == day
    assert source == "daily_close"


def _entry(df: pd.DataFrame, code: str, day, mode: str) -> tuple[float | None, object, str]:
    return entry_on_or_after(df, code, day, mode=mode, entry_time="", fallback="close", intraday_cache={})


def test_close_mode_skips_day_that_closed_at_limit_up() -> None:
    d0, d1, d2 = (datetime(2026, 1, day).date() for day in (5, 6, 7))
    df = pd.DataFrame(
        {
            "date": [d0, d1, d2],
            "open": [10.0, 10.6, 11.2],
            "high": [10.0, 11.0, 12.4],
            "low": [10.0, 10.4, 11.1],
            "close": [10.0, 11.0, 11.8],
        }
    )

    price, entry_date, source = _entry(df, "000001", d1, "close")

    assert price == 11.8
    assert entry_date == d2
    assert source == "daily_close"


def test_open_mode_skips_t_shaped_board_that_opened_locked_then_broke() -> None:
    """开盘一字封涨停、盘中被砸开：开盘价买不到，几何判据会漏掉这种票。"""
    d0, d1, d2 = (datetime(2026, 1, day).date() for day in (5, 6, 7))
    df = pd.DataFrame(
        {
            "date": [d0, d1, d2],
            "open": [10.0, 11.0, 10.9],
            "high": [10.0, 11.0, 11.4],
            "low": [10.0, 10.2, 10.7],
            "close": [10.0, 10.5, 11.2],
        }
    )

    price, entry_date, _source = _entry(df, "000001", d1, "open")

    assert price == 10.9
    assert entry_date == d2


def test_open_mode_buys_board_that_was_not_locked_at_the_open() -> None:
    """尾盘才封涨停：开盘竞价买得到，不该跳过。"""
    d0, d1 = (datetime(2026, 1, day).date() for day in (5, 6))
    df = pd.DataFrame(
        {
            "date": [d0, d1],
            "open": [10.0, 10.2],
            "high": [10.0, 11.0],
            "low": [10.0, 10.1],
            "close": [10.0, 11.0],
        }
    )

    price, entry_date, _source = _entry(df, "000001", d1, "open")

    assert price == 10.2
    assert entry_date == d1


def test_chinext_tolerates_ten_pct_open_because_limit_is_twenty() -> None:
    d0, d1 = (datetime(2026, 1, day).date() for day in (5, 6))
    df = pd.DataFrame(
        {
            "date": [d0, d1],
            "open": [10.0, 11.0],
            "high": [10.0, 11.6],
            "low": [10.0, 10.8],
            "close": [10.0, 11.5],
        }
    )

    price, _entry_date, _source = _entry(df, "300750", d1, "open")

    assert price == 11.0


def test_us_market_never_blocks_entry_on_price_limits() -> None:
    d0, d1 = (datetime(2026, 1, day).date() for day in (5, 6))
    df = pd.DataFrame(
        {
            "date": [d0, d1],
            "open": [10.0, 11.0],
            "high": [10.0, 11.0],
            "low": [10.0, 11.0],
            "close": [10.0, 11.0],
        }
    )

    price, entry_date, _source = entry_on_or_after(
        df, "AAPL", d1, mode="open", entry_time="", fallback="close", intraday_cache={}, market="us"
    )

    assert price == 11.0
    assert entry_date == d1


def test_tail_1455_fallback_close_uses_daily_close() -> None:
    day = datetime(2026, 1, 5).date()
    df = pd.DataFrame({"date": [day], "open": [10.0], "high": [10.8], "low": [9.8], "close": [10.5]})

    price, entry_date, source = entry_on_or_after(
        df,
        "000001",
        day,
        mode="tail_1455",
        entry_time="14:55",
        fallback="close",
        intraday_cache={},
    )

    assert price == 10.5
    assert entry_date == day
    assert source == "daily_close_fallback"


def test_tail_1455_fallback_skip_marks_missing() -> None:
    day = datetime(2026, 1, 5).date()
    df = pd.DataFrame({"date": [day], "open": [10.0], "high": [10.8], "low": [9.8], "close": [10.5]})

    price, entry_date, source = entry_on_or_after(
        df,
        "000001",
        day,
        mode="tail_1455",
        entry_time="14:55",
        fallback="skip",
        intraday_cache={},
    )

    assert price is None
    assert entry_date is None
    assert source == "tail_1455_missing_skip"


def test_tail_1455_uses_injected_intraday_fetcher() -> None:
    day = datetime(2026, 1, 5).date()
    df = pd.DataFrame({"date": [day], "open": [10.0], "high": [10.8], "low": [9.8], "close": [10.5]})

    def fetcher(_code, _day, entry_time, _cache):
        return 10.2, f"tickflow_1m_{entry_time}"

    price, entry_date, source = entry_on_or_after(
        df,
        "000001",
        day,
        mode="tail_1455",
        entry_time="14:55",
        fallback="close",
        intraday_cache={},
        intraday_price_fetcher=fetcher,
    )

    assert price == 10.2
    assert entry_date == day
    assert source == "tickflow_1m_14:55"


def test_calc_trade_excursion_uses_window_high_low() -> None:
    d1 = datetime(2026, 1, 5).date()
    d2 = datetime(2026, 1, 6).date()
    day_ohlc = {
        d1: (10.0, 11.0, 9.5, 10.6),
        d2: (10.6, 12.0, 9.0, 11.5),
    }

    mfe, mae = calc_trade_excursion_pct(day_ohlc, [d1, d2], 10.0)

    assert mfe == pytest.approx(20.0)
    assert mae == pytest.approx(-10.0)
