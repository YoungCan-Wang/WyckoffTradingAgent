"""Offline unit tests for the limit-up-hunter skill scanner (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "limit-up-hunter" / "scripts" / "ladder_scanner.py"
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location("ladder_scanner", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


LS = _load_scanner()


def _raw_quote(
    symbol: str,
    name: str,
    prev_close: float,
    open_p: float,
    high_p: float,
    low_p: float,
    last_p: float,
    vwap: float,
    amount_yi: float = 5.0,
) -> dict[str, Any]:
    amt = amount_yi * 1e8
    vol = amt / (vwap * 100.0)
    change_pct = (last_p - prev_close) / prev_close
    return {
        "symbol": symbol,
        "prev_close": prev_close,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "last_price": last_p,
        "volume": vol,
        "amount": amt,
        "ext": {"name": name, "change_pct": change_pct},
    }


def test_parse_quote_filters_st_and_computes_metrics():
    assert LS.parse_quote(_raw_quote("002501.SZ", "*ST利源", 1.10, 1.07, 1.09, 1.06, 1.09, 1.07)) is None
    assert LS.parse_quote({"symbol": "000001.SZ", "prev_close": 0, "ext": {"name": "平安银行"}}) is None

    m = LS.parse_quote(_raw_quote("002388.SZ", "新亚制程", 6.06, 5.75, 6.67, 5.71, 6.67, 6.18, 6.02))
    assert m is not None
    assert m.is_limit_up is True
    assert m.is_one_word is False
    assert m.open_pct == pytest.approx((5.75 - 6.06) / 6.06 * 100.0)
    assert m.diff_from_open == pytest.approx(15.18, abs=0.05)


def test_classify_ladder_buckets_separates_one_word_reversals_and_traps():
    quotes = [
        # 1. One-word limit-up (永臻股份)
        _raw_quote("603381.SH", "永臻股份", 18.85, 20.74, 20.74, 20.74, 20.74, 20.74, 1.20),
        # 2. Deep-water 2->3 turnover limit-up (新亚制程)
        _raw_quote("002388.SZ", "新亚制程", 6.06, 5.75, 6.67, 5.71, 6.67, 6.18, 6.02),
        # 3. Non-limit-up deep-water reversal holding above VWAP
        _raw_quote("688347.SH", "华虹宏力", 162.0, 158.0, 185.1, 155.0, 185.0, 168.7, 34.5),
        # 4. Mid-ladder spike-and-dump trap below VWAP (中利集团)
        _raw_quote("002309.SZ", "中利集团", 2.92, 2.88, 3.14, 2.76, 2.86, 2.98, 11.2),
        # 5. First-board limit-up trap below VWAP (常铝股份)
        _raw_quote("002160.SZ", "常铝股份", 3.62, 3.53, 3.98, 3.52, 3.68, 3.84, 2.2),
    ]
    parsed = [m for q in quotes if (m := LS.parse_quote(q)) is not None]
    buckets = LS.classify_ladder_buckets(parsed)

    assert [m.symbol for m in buckets["one_words"]] == ["603381.SH"]
    assert [m.symbol for m in buckets["turnover_lu"]] == ["002388.SZ"]
    assert [m.symbol for m in buckets["deep_reversals"]] == ["688347.SH"]
    assert [m.symbol for m in buckets["traps"]] == ["002309.SZ", "002160.SZ"]


def test_tag_keyword_candidate_flags_1030_laggard_trap(capsys):
    dongfang_quotes = [
        _raw_quote("002086.SZ", "东方海洋", 3.61, 3.67, 3.97, 3.67, 3.97, 3.81, 22.0),
        _raw_quote("002310.SZ", "东方新能", 2.11, 2.07, 2.32, 2.06, 2.32, 2.24, 2.4),
        _raw_quote("603366.SH", "日出东方", 8.17, 7.94, 8.99, 7.94, 8.99, 8.64, 4.5),
        _raw_quote("002611.SZ", "东方精工", 13.38, 12.99, 14.68, 12.85, 14.03, 13.84, 6.1),
        _raw_quote("002167.SZ", "东方锆业", 13.74, 13.12, 13.86, 13.06, 13.76, 13.43, 3.1),
    ]
    parsed = [m for q in dongfang_quotes if (m := LS.parse_quote(q)) is not None]
    LS.print_sections(parsed, keyword="东方", now_hhmm="10:32")
    out = capsys.readouterr().out

    assert "002086.SZ  东方海洋" in out and "[🔥前排封板先锋]" in out
    assert "002611.SZ  东方精工" in out and "[✅均线上活跃前排]" in out
    assert "002167.SZ  东方锆业" in out and "[⚠️10:30后平盘诱多禁入]" in out
