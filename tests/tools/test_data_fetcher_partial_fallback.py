from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from tools import data_fetcher


def test_partial_tickflow_result_retries_only_expected_trading_symbols(monkeypatch) -> None:
    frame = pd.DataFrame({"date": ["2026-07-15"], "close": [10.0]})
    monkeypatch.setattr(
        data_fetcher.tickflow_batch_fetcher,
        "fetch_tickflow_daily_batch",
        lambda **_kwargs: ({"000001": frame}, {"fetch_ok": 1, "fetch_fail": 2}),
    )
    monkeypatch.setattr(data_fetcher, "_suspended_symbols", lambda _day: {"000003"})
    captured = {}

    def fake_fallback(**kwargs):
        captured.update(kwargs)
        return {"000002": frame}, {"fetch_ok": 1, "fetch_fail": 0}

    monkeypatch.setattr(data_fetcher.ohlcv_fallback_fetcher, "fetch_ohlcv_fallback", fake_fallback)
    window = SimpleNamespace(start_trade_date=date(2026, 1, 1), end_trade_date=date(2026, 7, 15))

    result, stats = data_fetcher.fetch_all_ohlcv(["000001", "000002", "000003"], window)

    assert set(result) == {"000001", "000002"}
    assert captured["symbols"] == ["000002"]
    assert stats["suspended_symbols"] == ["000003"]
    assert stats["partial_fallback_ok"] == 1
    assert stats["raw_fetch_missing"] == 1
    assert stats["excluded_non_trading"] == 1
    assert stats["fetch_fail"] == 0
