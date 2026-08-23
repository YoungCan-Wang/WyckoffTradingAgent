from __future__ import annotations

from datetime import date

import pandas as pd

from core.asof_cut import cut_dated_records, cut_financial_map, cut_ohlcv_to_as_of


def test_ohlcv_requires_exact_as_of_bar():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-03", "2026-08-04", "2026-08-05"]),
            "close": [10.0, 11.0, 12.0],
        }
    )
    cut = cut_ohlcv_to_as_of(frame, date(2026, 8, 4))
    assert cut is not None
    assert list(cut["close"]) == [10.0, 11.0]
    assert cut_ohlcv_to_as_of(frame, date(2026, 8, 6)) is None


def test_dated_records_drop_future_and_undated():
    records = [
        {"name": "old", "announce_date": "2026-08-01"},
        {"name": "future", "filed_date": "2026-08-10"},
        {"name": "undated", "title": "x"},
    ]
    kept = cut_dated_records(records, "2026-08-04")
    assert [item["name"] for item in kept] == ["old"]


def test_funnel_cut_only_when_end_calendar_day_set(monkeypatch):
    """截止日期早于今天不是回放——否则周日主漏斗会被误裁。"""
    from types import SimpleNamespace

    from workflows.funnel_data import _funnel_is_historical, _maybe_cut_financial_map

    window = SimpleNamespace(end_trade_date=date(2026, 5, 22))
    monkeypatch.delenv("END_CALENDAR_DAY", raising=False)
    assert _funnel_is_historical(window) is False
    raw = {"000002": {"roe": 20, "filed_date": "2026-08-20"}}
    assert _maybe_cut_financial_map(raw, window) == raw

    monkeypatch.setenv("END_CALENDAR_DAY", "2026-05-22")
    assert _funnel_is_historical(window) is True
    assert _maybe_cut_financial_map(raw, window) == {}


def test_financial_map_fail_closed_without_date():
    raw = {
        "000001": {"roe": 12, "filed_date": "2026-08-01"},
        "000002": {"roe": 20, "filed_date": "2026-08-20"},
        "000003": {"roe": 8},
    }
    cut = cut_financial_map(raw, date(2026, 8, 4))
    assert list(cut) == ["000001"]
