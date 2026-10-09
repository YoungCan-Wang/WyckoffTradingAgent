from __future__ import annotations

import json
from datetime import date

import pandas as pd

from integrations import fetch_a_share_csv, market_metadata


class FakePro:
    def __init__(self):
        self.dates = []

    def daily_basic(self, trade_date, fields):
        self.dates.append(trade_date)
        if trade_date == "20260930":
            return pd.DataFrame({"ts_code": ["000001.SZ"], "float_share": [100.0], "total_mv": [10000.0]})
        return pd.DataFrame()


class HolidayToday(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 8)


def test_recent_metadata_uses_trading_dates_across_long_holiday(monkeypatch):
    monkeypatch.setattr(market_metadata, "date", HolidayToday)
    days = (date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))
    monkeypatch.setattr(fetch_a_share_csv, "cached_trade_dates", lambda: days)
    pro = FakePro()
    assert market_metadata._recent_daily_basic_map(pro, "float_share", 1e4) == {"000001": 1e6}
    assert pro.dates == ["20260930"]


def test_recent_metadata_retries_only_open_dates(monkeypatch):
    monkeypatch.setattr(market_metadata, "date", HolidayToday)
    days = (date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))
    monkeypatch.setattr(fetch_a_share_csv, "cached_trade_dates", lambda: days)

    class Pro:
        def __init__(self):
            self.dates = []

        def daily_basic(self, trade_date, fields):
            self.dates.append(trade_date)
            if trade_date == "20260930":
                raise OSError("temporary")
            if trade_date == "20260929":
                return pd.DataFrame({"ts_code": ["000001.SZ"], "float_share": [100.0]})
            return pd.DataFrame()

    pro = Pro()
    assert market_metadata._recent_daily_basic_map(pro, "float_share", 1e4) == {"000001": 1e6}
    assert pro.dates == ["20260930", "20260929"]


def test_target_date_float_share_does_not_use_current_cache(monkeypatch, tmp_path):
    cache = tmp_path / "float_share_cache.json"
    cache.write_text(json.dumps({"000001": 999999999}))
    monkeypatch.setattr(market_metadata, "FLOAT_SHARE_CACHE", cache)
    pro = FakePro()
    monkeypatch.setattr(market_metadata, "_tushare_pro", lambda: pro)
    assert market_metadata.fetch_float_share_map(as_of_date=date(2026, 9, 30)) == {"000001": 1e6}
    assert pro.dates == ["20260930"]
    assert json.loads(cache.read_text()) == {"000001": 999999999}


def test_missing_target_date_does_not_fall_back_to_future_cache(monkeypatch, tmp_path):
    cache = tmp_path / "float_share_cache.json"
    cache.write_text(json.dumps({"000001": 999999999}))
    monkeypatch.setattr(market_metadata, "FLOAT_SHARE_CACHE", cache)
    monkeypatch.setattr(market_metadata, "_tushare_pro", lambda: FakePro())
    assert market_metadata.fetch_float_share_map(as_of_date=date(2026, 9, 29)) == {}


def test_calendar_failure_does_not_use_natural_day_guess(monkeypatch):
    def boom():
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(fetch_a_share_csv, "cached_trade_dates", boom)
    pro = FakePro()
    assert market_metadata._recent_daily_basic_map(pro, "float_share", 1e4) == {}
    assert pro.dates == []
