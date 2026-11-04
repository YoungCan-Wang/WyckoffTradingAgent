"""Unit tests for the full-market 5-minute intraday radar script."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

from utils.trading_clock import CN_TZ, cn_today

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "intraday-radar" / "scripts" / "intraday_radar.py"
)


def _load_radar_module():
    spec = importlib.util.spec_from_file_location("intraday_radar", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_is_a_share_and_is_etf_exclude_indices():
    radar = _load_radar_module()
    assert not radar.is_a_share("000001.SH")
    assert not radar.is_a_share("000300.SH")
    assert not radar.is_a_share("000688.SH")
    assert not radar.is_a_share("399001.SZ")
    assert not radar.is_a_share("399006.SZ")
    assert radar.is_a_share("000001.SZ")
    assert radar.is_a_share("300750.SZ")
    assert radar.is_a_share("600519.SH")
    assert radar.is_a_share("688981.SH")
    assert radar.is_a_share("830799.BJ")
    assert radar.is_etf("588170.SH")
    assert radar.is_etf("159915.SZ")
    assert not radar.is_etf("600519.SH")


def test_index_never_counted_in_breadth_or_l2_map():
    radar = _load_radar_module()
    today = cn_today().isoformat()
    prev_state = {
        "date": today,
        "red": 0,
        "quotes": {"000001.SH": {"pct": 1.0, "amt_yi": 5000.0}},
    }
    curr_quotes = {
        "000001.SH": {
            "symbol": "000001.SH",
            "last_price": 3635.0,
            "prev_close": 3500.0,
            "volume": 1e9,
            "amount": 5500e8,
            "ext": {"name": "上证指数"},
        }
    }
    l2_map = {
        "000001": {
            "name": "平安银行",
            "sector": "银行",
            "rps": 20.0,
            "l2_eligible": True,
            "channels": ["地量蓄势"],
            "prev_amt_yi": 10.0,
        }
    }
    level, _, body, red_count, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, l2_map, {}, now_ts=1000.0)
    assert red_count == 0
    assert level == "L1"
    assert "平安银行" not in body


def test_first_scan_without_baseline_suppresses_5m_deltas():
    radar = _load_radar_module()
    curr_quotes = {
        "688001.SH": {
            "symbol": "688001.SH",
            "last_price": 10.4,
            "prev_close": 10.0,
            "volume": 1e7,
            "amount": 1.035e8,
            "ext": {"name": "测试芯片"},
        }
    }
    l2_map = {
        "688001": {
            "name": "测试芯片",
            "sector": "半导体",
            "rps": 12.0,
            "l2_eligible": True,
            "channels": ["地量蓄势"],
            "prev_amt_yi": 1.5,
        }
    }
    level, _, _, red_count, _ = radar.analyze_snapshot_diff(curr_quotes, {}, l2_map, {}, now_ts=1000.0)
    assert red_count == 1
    assert level == "L1"


def test_second_scan_triggers_l3_and_enforces_30m_cooldown():
    radar = _load_radar_module()
    today = cn_today().isoformat()
    prev_state = {
        "date": today,
        "red": 1,
        "cooldowns": {},
        "quotes": {"688001.SH": {"pct": 1.0, "amt_yi": 0.50}},
    }
    curr_quotes = {
        "688001.SH": {
            "symbol": "688001.SH",
            "last_price": 10.35,
            "prev_close": 10.0,
            "volume": 1e7,
            "amount": 1.03e8,
            "ext": {"name": "测试芯片"},
        }
    }
    l2_map = {
        "688001": {
            "name": "测试芯片",
            "sector": "半导体",
            "rps": 12.0,
            "l2_eligible": True,
            "channels": ["地量蓄势"],
            "prev_amt_yi": 2.0,
        }
    }
    level1, _, body1, _, cd1 = radar.analyze_snapshot_diff(curr_quotes, prev_state, l2_map, {}, now_ts=1000.0)
    assert level1 == "L3"
    assert "688001.SH" in body1

    prev_state["cooldowns"] = cd1
    level2, _, _, _, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, l2_map, {}, now_ts=1300.0)
    assert level2 == "L1"


def test_trading_session_and_send_cadence(monkeypatch):
    radar = _load_radar_module()
    monkeypatch.setattr(radar, "is_a_share_trading_day", lambda d: True)
    t_open = datetime(2026, 11, 4, 10, 0, tzinfo=CN_TZ)
    t_mid = datetime(2026, 11, 4, 10, 5, tzinfo=CN_TZ)
    t_lunch = datetime(2026, 11, 4, 12, 15, tzinfo=CN_TZ)
    t_after = datetime(2026, 11, 4, 16, 0, tzinfo=CN_TZ)

    assert radar.is_cn_trading_session(t_open)
    assert not radar.is_cn_trading_session(t_lunch)
    assert not radar.is_cn_trading_session(t_after)

    assert radar.should_send_card("L1", t_open, force_send=False, urgent_only=False)
    assert not radar.should_send_card("L1", t_mid, force_send=False, urgent_only=False)
    assert radar.should_send_card("L1", t_mid, force_send=True, urgent_only=False)
    assert not radar.should_send_card("L1", t_open, force_send=True, urgent_only=True)
    assert radar.should_send_card("L3", t_mid, force_send=False, urgent_only=True)
