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
    timestamp: int = 1793932320000,
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
        "timestamp": timestamp,
        "ext": {"name": name, "change_pct": change_pct},
    }


def test_parse_quote_filters_st_bj_new_ipo_and_checks_exact_one_word():
    assert LS.parse_quote(_raw_quote("002501.SZ", "*ST利源", 1.10, 1.07, 1.09, 1.06, 1.09, 1.07)) is None
    assert LS.parse_quote(_raw_quote("920085.BJ", "森峰科技", 10.0, 25.0, 28.0, 22.0, 25.1, 25.0)) is None
    assert LS.parse_quote(_raw_quote("301999.SZ", "N森峰", 10.0, 25.0, 28.0, 22.0, 25.1, 25.0)) is None
    assert LS.parse_quote(_raw_quote("688999.SH", "C新股", 20.0, 35.0, 38.0, 32.0, 35.1, 35.0)) is None

    # High-open (+9.6%) that seals limit-up is NOT a one-word board
    near_ow = LS.parse_quote(_raw_quote("000001.SZ", "测试高开", 10.00, 10.96, 11.00, 10.94, 11.00, 10.98))
    assert near_ow is not None
    assert near_ow.is_limit_up is True
    assert near_ow.is_one_word is False

    # True one-word board with tiny floating-point VWAP drift still shows >=VWAP
    ow_q = _raw_quote("603381.SH", "永臻股份", 18.85, 20.74, 20.74, 20.74, 20.74, 20.7400002, 1.20)
    ow = LS.parse_quote(ow_q)
    assert ow is not None
    assert ow.is_limit_up is True
    assert ow.is_one_word is True
    assert ow.above_vwap is True
    assert ">=VWAP: 20.74" in LS.format_row(ow)


def test_compute_ladder_from_daily_rows_and_5m_analysis(capsys, tmp_path, monkeypatch):
    # Generate a 9-consecutive-board monster stock across 10 trading days (untruncated >6 boards)
    nine_board_rows: list[tuple[str, float, float]] = [("20261020", 10.00, 10.00)]
    p = 10.00
    for i in range(1, 10):
        nxt = LS.calc_limit_up_price(p, "002852.SZ")
        nine_board_rows.append((f"202610{20 + i:02d}", p, nxt))
        p = nxt

    rows_by_sym = {
        "002852.SZ": nine_board_rows,
        "002388.SZ": [
            ("20261103", 5.00, 5.00),
            ("20261104", 5.00, 5.50),
            ("20261105", 5.50, 6.05),
        ],
        "002086.SZ": [
            ("20261029", 2.50, 2.75),
            ("20261030", 2.75, 3.03),
            ("20261031", 3.03, 3.33),
            ("20261103", 3.33, 3.40),
            ("20261104", 3.40, 3.28),
            ("20261105", 3.28, 3.61),
        ],
    }
    ladder = LS.compute_ladder_from_daily_rows(rows_by_sym)
    assert ladder["002852.SZ"] == {"prev_boards": 9, "m_days_n_boards": ""}
    assert ladder["002388.SZ"] == {"prev_boards": 2, "m_days_n_boards": ""}
    assert ladder["002086.SZ"] == {"prev_boards": 1, "m_days_n_boards": "6天4板"}

    # Verify missing TUSHARE_TOKEN emits [WARN] to stderr
    monkeypatch.setattr(LS.Path, "home", lambda: tmp_path)
    assert LS.load_prev_ladder("20990101", None) == {}
    assert "[WARN]" in capsys.readouterr().err

    import pandas as pd

    bars_df = pd.DataFrame(
        [
            {"date": "2026-11-06", "open": 5.75, "high": 6.18, "close": 6.12, "amount": 1000.0},
            {"date": "2026-11-06", "open": 6.12, "high": 6.15, "close": 6.08, "amount": 120.0},
            {"date": "2026-11-06", "open": 6.08, "high": 6.25, "close": 6.25, "amount": 600.0},
        ]
    )
    summary = LS.analyze_5m_bars(bars_df, prev_close=6.06)
    assert summary is not None
    assert summary.first_5m_red is True
    assert summary.first_5m_surge_pct == pytest.approx((6.18 - 5.75) / 6.06 * 100.0)
    assert summary.min_vol_ratio == pytest.approx(0.12)
    assert summary.broke_morning_high is True
    assert "(<=15%锁仓)" in summary.format_tag()
    assert "过早高" in summary.format_tag()


def test_classify_ladder_buckets_retains_prev_pool_and_scales_20cm():
    ladder_info = {
        "603381.SH": {"prev_boards": 3, "m_days_n_boards": ""},
        "002388.SZ": {"prev_boards": 2, "m_days_n_boards": ""},
        "002309.SZ": {"prev_boards": 2, "m_days_n_boards": ""},
        "600481.SH": {"prev_boards": 1, "m_days_n_boards": ""},
    }
    quotes = [
        _raw_quote("603381.SH", "永臻股份", 18.85, 20.74, 20.74, 20.74, 20.74, 20.74, 1.20),
        _raw_quote("002388.SZ", "新亚制程", 6.06, 5.75, 6.67, 5.71, 6.67, 6.18, 6.02),
        # 1->2 stock opening deep underwater and staying underwater: still 100% retained in prev_ladder_pool
        _raw_quote("600481.SH", "双良节能", 4.10, 3.85, 3.92, 3.80, 3.88, 3.87, 3.10),
        # 20cm mild bounce (+6% from open, last +1%): filtered out of 20cm first-board reversals
        _raw_quote("301310.SZ", "鑫宏业", 50.0, 47.0, 50.8, 46.8, 50.5, 49.5, 2.5),
        # 20cm strong deep-water reversal (open -3.0%, last +14.2%, diff_from_open +17.2% >= 11%)
        _raw_quote("688347.SH", "华虹宏力", 162.0, 157.0, 185.1, 155.0, 185.0, 168.7, 34.5),
        # Mid-ladder 2->3 spike-and-dump below VWAP
        _raw_quote("002309.SZ", "中利集团", 2.92, 2.88, 3.14, 2.76, 2.86, 2.98, 11.2),
    ]
    parsed = [m for q in quotes if (m := LS.parse_quote(q, ladder_info)) is not None]
    buckets = LS.classify_ladder_buckets(parsed)

    assert [m.symbol for m in buckets["prev_ladder_pool"]] == [
        "603381.SH",
        "002388.SZ",
        "002309.SZ",
        "600481.SH",
    ]
    assert [m.symbol for m in buckets["one_words"]] == ["603381.SH"]
    assert [m.symbol for m in buckets["turnover_lu"]] == ["002388.SZ"]
    assert [m.symbol for m in buckets["deep_reversals"]] == ["002388.SZ", "688347.SH"]
    assert [m.symbol for m in buckets["traps"]] == ["002309.SZ"]


def test_tag_keyword_candidate_flags_1030_laggard_per_line(capsys):
    # 4 front-row limit-ups + 1 active front-runner + 1 flat laggard after 10:30
    dongfang_quotes = [
        _raw_quote("002086.SZ", "东方海洋", 3.61, 3.67, 3.97, 3.67, 3.97, 3.81, 22.0),
        _raw_quote("002310.SZ", "东方新能", 2.11, 2.07, 2.32, 2.06, 2.32, 2.24, 2.4),
        _raw_quote("603366.SH", "日出东方", 8.17, 7.94, 8.99, 7.94, 8.99, 8.64, 4.5),
        _raw_quote("300118.SZ", "东方日升", 12.00, 12.10, 14.40, 12.05, 14.40, 13.50, 8.2),
        _raw_quote("002611.SZ", "东方精工", 13.38, 12.99, 14.68, 12.85, 14.03, 13.84, 6.1),
        _raw_quote("002167.SZ", "东方锆业", 13.74, 13.12, 13.86, 13.06, 13.76, 13.43, 3.1),
    ]
    snap_dt = LS.extract_snapshot_time(dongfang_quotes)
    assert snap_dt.tzinfo is not None
    parsed = [m for q in dongfang_quotes if (m := LS.parse_quote(q)) is not None]
    LS.print_sections(parsed, keyword="东方", now_hhmm="10:32")
    lines = capsys.readouterr().out.splitlines()

    assert any("002086.SZ" in ln and "[🔥前排封板先锋]" in ln for ln in lines)
    assert any("002611.SZ" in ln and "[✅均线上活跃前排]" in ln for ln in lines)
    assert any("002167.SZ" in ln and "[⚠️10:30后平盘观察]" in ln for ln in lines)
