from __future__ import annotations

import pandas as pd

from integrations import index_data_source
from integrations.index_data_source import _fetch_index_tushare


class _FakePro:
    def index_daily(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "trade_date": ["20260715", "20260714", "20260710"],
                "open": [10.0, 9.8, 9.5],
                "high": [10.2, 10.0, 9.7],
                "low": [9.9, 9.7, 9.4],
                "close": [10.1, 9.9, 9.6],
                "vol": [100.0, 90.0, 80.0],
                "pct_chg": [2.0, 3.0, 1.0],
            }
        )


def test_fetch_index_tushare_normalizes_descending_dates(monkeypatch) -> None:
    monkeypatch.setattr("integrations.tushare_client.get_pro", lambda: _FakePro())

    result = _fetch_index_tushare("000001", "20260710", "20260715")

    assert result["date"].tolist() == ["2026-07-10", "2026-07-14", "2026-07-15"]


def test_fetch_index_hist_retries_both_sources(monkeypatch) -> None:
    calls = {"tushare": 0}

    def fake_tushare(*_args):
        calls["tushare"] += 1
        if calls["tushare"] == 1:
            raise OSError("temporary")
        return pd.DataFrame({"date": ["2026-07-15"]})

    monkeypatch.setenv("INDEX_DATA_MAX_RETRIES", "2")
    monkeypatch.setenv("INDEX_DATA_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setattr(index_data_source, "_fetch_index_tushare", fake_tushare)
    monkeypatch.setattr(index_data_source, "fetch_index_akshare", lambda *_args: (_ for _ in ()).throw(OSError()))

    result = index_data_source.fetch_index_hist("000001", "20260710", "20260715")

    assert result["date"].tolist() == ["2026-07-15"]
    assert calls["tushare"] == 2
