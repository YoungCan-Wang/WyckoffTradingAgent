from __future__ import annotations

import pandas as pd

from core.ohlc_guard import dirty_bar_reason, sanitize_ohlcv_frame, sanitize_ohlcv_map


def _bar(**overrides) -> dict:
    row = {"date": "2026-08-01", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000}
    row.update(overrides)
    return row


def test_clean_bar_has_no_reason():
    assert dirty_bar_reason(pd.Series(_bar())) is None


def test_keep_row_nullifies_dirty_values():
    """单股分析要留日期行，不能把整根 K 线丢掉。"""
    frame, reasons = sanitize_ohlcv_frame(
        pd.DataFrame([_bar(open="bad", high=float("inf"), low=float("-inf"), close=float("nan"))]),
        drop=False,
    )
    assert len(frame) == 1
    assert pd.isna(frame.iloc[0]["close"])
    assert reasons["nan_ohlc"] == 1


def test_high_below_low_is_dropped():
    frame, reasons = sanitize_ohlcv_frame(pd.DataFrame([_bar(), _bar(high=8.0, low=9.5)]))
    assert len(frame) == 1
    assert reasons["high_lt_low"] == 1


def test_sanitize_map_drops_all_dirty_symbol(monkeypatch):
    monkeypatch.delenv("OHLCV_DIRTY_BAR_GUARD", raising=False)
    dirty = pd.DataFrame([_bar(high=1.0, low=2.0)])
    clean = pd.DataFrame([_bar()])
    out, stats = sanitize_ohlcv_map({"000001": dirty, "000002": clean}, {"fetch_ok": 2})
    assert list(out) == ["000002"]
    assert stats["dirty_symbols_dropped"] == 1
    assert stats["dirty_bars_dropped"] == 1


def test_guard_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OHLCV_DIRTY_BAR_GUARD", "0")
    dirty = pd.DataFrame([_bar(high=1.0, low=2.0)])
    out, stats = sanitize_ohlcv_map({"000001": dirty})
    assert "000001" in out
    assert stats["dirty_bar_guard"] == "off"
