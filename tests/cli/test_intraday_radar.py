"""Unit tests for the full-market 5-minute intraday radar script."""

from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

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
        "ts": 900.0,
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
            "l3_eligible": True,
            "channels": ["地量蓄势"],
        }
    }
    level, _, body, red_count, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, l2_map, {}, now_ts=1000.0)
    assert red_count == 0
    assert level == "L1"
    assert "平安银行" not in body


def test_feishu_send_uses_lark_cli_same_bot_and_checks_failure(monkeypatch):
    radar = _load_radar_module()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "+messages-send" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"code": 0, "data": {"message_id": "om_test_123"}}),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(radar.subprocess, "run", fake_run)
    msg_id = radar.send_feishu_tiered_alert("Title", "Body", level="L4", user_open_id="ou_mock_user", dry_run=False)
    assert msg_id == "om_test_123"
    assert len(calls) == 2
    assert calls[0][:5] == ["lark-cli", "im", "+messages-send", "--as", "bot"]
    assert calls[1][:5] == ["lark-cli", "im", "messages", "urgent_phone", "--as"]

    monkeypatch.setattr(
        radar.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="bot error"),
    )
    assert (
        radar.send_feishu_tiered_alert("Title", "Body", level="L3", user_open_id="ou_mock_user", dry_run=False) is None
    )


def test_holdings_stop_loss_fires_on_zero_volume_open_without_cooldown_downgrade():
    radar = _load_radar_module()
    today = cn_today().isoformat()
    holdings = {"600000.SH": {"cost": 10.0, "stop": 9.50}}
    prev_state = {
        "date": today,
        "ts": 1000.0,
        "red": 0,
        "cooldowns": {},
        "quotes": {"600000.SH": {"pct": -1.0, "amt_yi": 0.01}},
    }
    curr_quotes = {
        "600000.SH": {
            "symbol": "600000.SH",
            "last_price": 9.40,
            "prev_close": 10.0,
            "volume": 1000,
            "amount": 9400.0,
            "high": 9.80,
            "low": 9.40,
            "ext": {"name": "浦发银行"},
        }
    }
    level1, _, body1, _, cd1 = radar.analyze_snapshot_diff(curr_quotes, prev_state, {}, holdings, now_ts=1300.0)
    assert level1 == "L4"
    assert "L4 破位止损" in body1

    prev_state["cooldowns"] = cd1
    prev_state["ts"] = 1300.0
    level2, _, _, _, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, {}, holdings, now_ts=1600.0)
    assert level2 == "L1"
    # Even 2 hours (7200s) later on the same trading day, L4 stop-loss stays cooled down (86400s window)
    prev_state["ts"] = 8500.0
    level3, _, _, _, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, {}, holdings, now_ts=8800.0)
    assert level3 == "L1"


def test_holdings_stop_loss_matches_bare_six_digit_portfolio_codes():
    """Portfolio codes are bare 6-digit; TickFlow quotes are suffixed — must still L4."""
    radar = _load_radar_module()
    holdings = radar.parse_holdings_arg(["600000:10.0:9.50", "000001:12.0:11.0", "830799:8.0:7.2"])
    assert holdings == {
        "600000.SH": {"cost": 10.0, "stop": 9.50},
        "000001.SZ": {"cost": 12.0, "stop": 11.0},
        "830799.BJ": {"cost": 8.0, "stop": 7.2},
    }
    curr_quotes = {
        "600000.SH": {
            "symbol": "600000.SH",
            "last_price": 9.40,
            "prev_close": 10.0,
            "volume": 1000,
            "amount": 9400.0,
            "ext": {"name": "浦发银行"},
        }
    }
    level, _, body, _, _ = radar.analyze_snapshot_diff(curr_quotes, {}, {}, holdings, now_ts=1300.0)
    assert level == "L4"
    assert "L4 破位止损" in body
    assert "600000.SH" in body


def test_springboard_l4_does_not_require_l2_eligible():
    """A+B+C / launchpad L4 must fire even when review_trace row has l2_eligible=False."""
    radar = _load_radar_module()
    today = cn_today().isoformat()
    prev_state = {"date": today, "ts": 1000.0, "red": 0, "cooldowns": {}, "quotes": {}}
    curr_quotes = {}
    for i in range(20):
        sym = f"{600100 + i:06d}.SH"
        prev_state["quotes"][sym] = {"pct": 1.0, "amt_yi": 1.0}
        curr_quotes[sym] = {
            "symbol": sym,
            "last_price": 10.2,
            "prev_close": 10.0,
            "volume": 1e7,
            "amount": 1.02e8,
            "ext": {"name": f"红{i}"},
        }
    prev_state["quotes"]["688001.SH"] = {"pct": 1.0, "amt_yi": 1.0}
    curr_quotes["688001.SH"] = {
        "symbol": "688001.SH",
        "last_price": 10.40,
        "prev_close": 10.0,
        "volume": 2e7,
        "amount": 2.08e8,
        "ext": {"name": "启动板"},
    }
    l2_map = {
        "688001": {
            "name": "启动板",
            "sector": "半导体",
            "rps": 15.0,
            "l2_eligible": False,
            "l3_eligible": False,
            "is_candidate": True,
            "channels": [],
            "is_springboard": True,
        }
    }
    level, _, body, _, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, l2_map, {}, now_ts=1300.0)
    assert level == "L4"
    assert "L4 满级起跳板突破" in body


def test_missing_holding_quote_is_not_silent_and_backfill_restores_stop():
    """Universe omission of a holding must warn (L2) and backfill can still fire L4 stop."""
    radar = _load_radar_module()
    today = cn_today().isoformat()
    holdings = {"600000.SH": {"cost": 10.0, "stop": 9.50}}
    prev_state = {
        "date": today,
        "ts": 1000.0,
        "quotes": {"600001.SH": {"pct": 1.0, "amt_yi": 0.5}, "600000.SH": {"pct": -1.0, "amt_yi": 0.5}},
    }
    gap_quotes = {
        "600001.SH": {
            "symbol": "600001.SH",
            "last_price": 10.1,
            "prev_close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "ext": {"name": "其他"},
        }
    }
    level_gap, _, body_gap, _, _ = radar.analyze_snapshot_diff(gap_quotes, prev_state, {}, holdings, now_ts=1200.0)
    assert level_gap == "L2"
    assert "持仓行情缺口" in body_gap
    assert "L4 破位止损" not in body_gap
    assert "无法判定" in body_gap

    holding_row = {
        "symbol": "600000.SH",
        "last_price": 9.40,
        "prev_close": 10.0,
        "volume": 1000,
        "amount": 9400.0,
        "ext": {"name": "浦发银行"},
    }

    class FakeClient:
        def get_quotes(self, symbols=None, universes=None):
            assert symbols == ["600000.SH"]
            return {"600000.SH": holding_row}

    merged = radar._backfill_holding_quotes(FakeClient(), gap_quotes, holdings)
    assert "600000.SH" in merged
    level_ok, _, body_ok, _, _ = radar.analyze_snapshot_diff(merged, prev_state, {}, holdings, now_ts=1300.0)
    assert level_ok == "L4"
    assert "L4 破位止损" in body_ok


def test_etf_and_unrelated_stock_moves_do_not_spam_l2_urgent():
    radar = _load_radar_module()
    today = cn_today().isoformat()
    prev_state = {
        "date": today,
        "ts": 1000.0,
        "red": 10,
        "cooldowns": {},
        "quotes": {
            "588170.SH": {"pct": 1.0, "amt_yi": 2.0},
            "600001.SH": {"pct": 0.0, "amt_yi": 1.0},
            "600002.SH": {"pct": 0.0, "amt_yi": 1.0},
            "600003.SH": {"pct": 0.0, "amt_yi": 1.0},
        },
    }
    curr_quotes = {
        "588170.SH": {
            "symbol": "588170.SH",
            "last_price": 1.025,
            "prev_close": 1.0,
            "volume": 3e8,
            "amount": 3.05e8,
            "ext": {"name": "半导体ETF"},
        },
        "600001.SH": {
            "symbol": "600001.SH",
            "last_price": 10.3,
            "prev_close": 10.0,
            "volume": 1.5e7,
            "amount": 1.5e8,
            "ext": {"name": "甲股"},
        },
        "600002.SH": {
            "symbol": "600002.SH",
            "last_price": 10.3,
            "prev_close": 10.0,
            "volume": 1.5e7,
            "amount": 1.5e8,
            "ext": {"name": "乙股"},
        },
        "600003.SH": {
            "symbol": "600003.SH",
            "last_price": 10.3,
            "prev_close": 10.0,
            "volume": 1.5e7,
            "amount": 1.5e8,
            "ext": {"name": "丙股"},
        },
    }
    l2_map = {
        "600001": {"sector": "医药"},
        "600002": {"sector": "煤炭"},
        "600003": {"sector": "农业"},
    }
    level, _, body, _, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, l2_map, {}, now_ts=1300.0)
    assert level == "L1"
    assert "588170.SH" in body

    same_sec_map = {
        "600001": {"sector": "半导体"},
        "600002": {"sector": "半导体"},
        "600003": {"sector": "半导体"},
    }
    level_sec, _, body_sec, _, cd_sec = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, same_sec_map, {}, now_ts=1300.0
    )
    assert level_sec == "L2"
    assert "半导体 板块5m共振" in body_sec

    prev_state["cooldowns"] = cd_sec
    level_sec2, _, _, _, _ = radar.analyze_snapshot_diff(curr_quotes, prev_state, same_sec_map, {}, now_ts=1500.0)
    assert level_sec2 == "L1"


def test_midday_restart_gap_resets_baseline_and_lunch_bridge_preserves_it():
    radar = _load_radar_module()
    today = cn_today()
    t_1000 = datetime(today.year, today.month, today.day, 10, 0, tzinfo=CN_TZ).timestamp()
    t_1400 = datetime(today.year, today.month, today.day, 14, 0, tzinfo=CN_TZ).timestamp()
    t_1130 = datetime(today.year, today.month, today.day, 11, 30, tzinfo=CN_TZ).timestamp()
    t_1300 = datetime(today.year, today.month, today.day, 13, 0, tzinfo=CN_TZ).timestamp()

    state_1000 = {"date": today.isoformat(), "ts": t_1000, "quotes": {"688001.SH": {"pct": 0.5, "amt_yi": 0.5}}}
    assert not radar._is_fresh_baseline(state_1000, today.isoformat(), t_1400)

    state_1130 = {"date": today.isoformat(), "ts": t_1130, "quotes": {"688001.SH": {"pct": 0.5, "amt_yi": 0.5}}}
    assert radar._is_fresh_baseline(state_1130, today.isoformat(), t_1300)


def test_real_trace_schema_and_stale_trace_guard(tmp_path, monkeypatch):
    radar = _load_radar_module()
    today_compact = cn_today().strftime("%Y%m%d")
    trace_file = tmp_path / f"review_trace_{today_compact}.json.gz"
    rec_file = tmp_path / f"recommendation_tracking_{today_compact}.json"
    old_rec_file = tmp_path / "recommendation_tracking_20260501.json"

    trace_payload = {
        "trade_date": cn_today().isoformat(),
        "symbols": {
            "688001": {
                "name": "测试芯片",
                "sector": "半导体",
                "rps_fast": 11.2,
                "l2_eligible": True,
                "l3_eligible": True,
                "l2_channel": "地量蓄势",
                "stage": "候选池已捕获",
                "entry": {"entry_type": "launchpad"},
                "reason": "候选车道: 启动平台",
            },
            "300001": {
                "name": "起跳测试",
                "sector": "通信设备",
                "rps_fast": 82.0,
                "l2_eligible": True,
                "l3_eligible": False,
                "l2_channel": "主升通道",
                "stage": "买点未确认",
            },
            "600999": {
                "name": "五个月前老票",
                "sector": "券商",
                "rps_fast": 50.0,
                "l2_eligible": True,
                "l3_eligible": False,
                "l2_channel": "主升通道",
                "stage": "买点未确认",
            },
        },
    }
    with gzip.open(trace_file, "wt", encoding="utf-8") as f:
        json.dump(trace_payload, f)
    rec_file.write_text(
        json.dumps({"rows": [{"code": "300001", "springboard_combo": "A+B+C"}]}),
        encoding="utf-8",
    )
    old_rec_file.write_text(
        json.dumps({"rows": [{"code": "600999", "springboard_combo": "A+B+C"}]}),
        encoding="utf-8",
    )

    l2_map = radar.load_wyckoff_l2_map(str(trace_file))
    assert l2_map["688001"]["is_springboard"] is True
    assert l2_map["688001"]["l3_eligible"] is True
    assert l2_map["688001"]["channels"] == ["地量蓄势"]
    assert l2_map["300001"]["is_springboard"] is True
    assert l2_map["600999"]["is_springboard"] is False

    # Holiday trading-day check: 2026-09-30 -> 2026-10-08 (8 calendar days, only 1 trading day)
    monkeypatch.setattr(radar, "is_a_share_trading_day", lambda d: d == date(2026, 10, 8))
    assert radar._trading_days_since(date(2026, 9, 30), date(2026, 10, 8)) == 1

    stale_file = tmp_path / "review_trace_20250101.json.gz"
    with gzip.open(stale_file, "wt", encoding="utf-8") as f:
        json.dump({"trade_date": "2025-01-01", "symbols": trace_payload["symbols"]}, f)
    assert radar.load_wyckoff_l2_map(str(stale_file), max_age_days=3) == {}


def test_l3_ignition_and_l1_drift_immune_cadence(tmp_path):
    radar = _load_radar_module()
    today = cn_today().isoformat()
    alert_log = tmp_path / "radar_alerts.jsonl"
    t_dir = tmp_path / "turnover"
    prev_state = {
        "date": today,
        "ts": 1000.0,
        "red": 1,
        "cooldowns": {},
        "quotes": {"688001.SH": {"pct": 1.0, "amt_yi": 0.80}},
    }
    curr_quotes = {
        "688001.SH": {
            "symbol": "688001.SH",
            "last_price": 10.35,
            "prev_close": 10.0,
            "volume": 1.2e7,
            "amount": 1.236e8,
            "ext": {"name": "测试芯片"},
        }
    }
    l2_map = {
        "688001": {
            "name": "测试芯片",
            "sector": "半导体",
            "rps": 12.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "is_springboard": False,
        }
    }
    # Wyckoff L2+L3 dry-volume ignition triggers L3 SMS alert and logs to jsonl
    level1, _, body1, _, cd1 = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, {}, now_ts=1200.0, turnover_dir=t_dir, alert_log_path=alert_log
    )
    assert level1 == "L3"
    assert "688001.SH" in body1
    logged = [json.loads(line) for line in alert_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(logged) == 1
    assert logged[0]["kind"] == "wyckoff_l2l3_ignition"
    assert logged[0]["level"] == "L3"
    assert logged[0]["symbol"] == "688001.SH"

    prev_state["cooldowns"] = cd1
    level2, _, _, _, _ = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, {}, now_ts=1400.0, turnover_dir=t_dir
    )
    assert level2 == "L1"

    assert radar.should_send_card("L1", now_ts=2000.0, last_l1_ts=0.0, force_send=False, urgent_only=False)
    assert not radar.should_send_card("L1", now_ts=3000.0, last_l1_ts=2000.0, force_send=False, urgent_only=False)
    assert radar.should_send_card("L1", now_ts=3800.0, last_l1_ts=2000.0, force_send=False, urgent_only=False)


def test_resolve_lark_open_id_and_parse_holdings_arg(monkeypatch):
    radar = _load_radar_module()
    monkeypatch.delenv("FEISHU_USER_OPEN_ID", raising=False)
    monkeypatch.setattr(
        radar.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"identities": {"user": {"openId": "ou_from_lark_cli"}}}),
            stderr="",
        ),
    )
    assert radar.resolve_lark_open_id() == "ou_from_lark_cli"
    assert radar.parse_holdings_arg(["600000.SH:10.5:9.8"]) == {"600000.SH": {"cost": 10.5, "stop": 9.8}}
    assert radar.parse_holdings_arg(["600000:10.5:9.8"]) == {"600000.SH": {"cost": 10.5, "stop": 9.8}}


def test_failed_send_does_not_persist_cooldown_in_run_once(tmp_path, monkeypatch):
    radar = _load_radar_module()
    state_file = tmp_path / "state.json"
    alert_log = tmp_path / "radar_alerts.jsonl"
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", alert_log)
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {})
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a, **kw: "ou_test")

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return {
                "600000.SH": {
                    "symbol": "600000.SH",
                    "last_price": 9.40,
                    "prev_close": 10.0,
                    "volume": 1000,
                    "amount": 9400.0,
                    "ext": {"name": "浦发银行"},
                }
            }

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    args = SimpleNamespace(
        ignore_hours=True,
        dry_run=False,
        trace=None,
        holding=["600000.SH:10.0:9.50"],
        user_id="ou_test",
        force_send=False,
        urgent_only=False,
    )

    # First run: Feishu send fails -> cooldown must NOT be saved AND alert log must NOT be written
    monkeypatch.setattr(radar, "send_feishu_tiered_alert", lambda *a, **kw: None)
    radar.run_once(args)
    saved1 = json.loads(state_file.read_text(encoding="utf-8"))
    assert "L4:holding_stop:600000.SH" not in saved1["cooldowns"]
    assert not alert_log.exists()

    # Second run: Feishu send succeeds -> cooldown IS saved AND alert log records sent=True, msg_id=om_ok_1
    monkeypatch.setattr(radar, "send_feishu_tiered_alert", lambda *a, **kw: "om_ok_1")
    radar.run_once(args)
    saved2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert "L4:holding_stop:600000.SH" in saved2["cooldowns"]
    rows = [json.loads(line) for line in alert_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["sent"] is True
    assert rows[0]["msg_id"] == "om_ok_1"


def test_failed_send_keeps_quote_baseline_so_holding_plunge_retries(tmp_path, monkeypatch):
    """Card send fail must not advance quotes — else Δpct L3 is zeroed and never retries."""
    import time

    radar = _load_radar_module()
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", tmp_path / "radar_alerts.jsonl")
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {})
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a, **kw: "ou_test")

    seed_ts = time.time() - 60
    state_file.write_text(
        json.dumps(
            {
                "date": cn_today().isoformat(),
                "ts": seed_ts,
                "last_l1_ts": 0,
                "red": 0,
                "cooldowns": {},
                "quotes": {
                    "600000.SH": {"pct": 0.0, "amt_yi": 1.0},
                    "000001.SH": {"pct": 0.0, "amt_yi": 10.0},
                    "399001.SZ": {"pct": 0.0, "amt_yi": 10.0},
                },
            }
        ),
        encoding="utf-8",
    )

    plunge_quotes = {
        "600000.SH": {
            "symbol": "600000.SH",
            "last_price": 9.5,
            "prev_close": 10.0,
            "volume": 1e8,
            "amount": 9.7e8,
            "ext": {"name": "浦发银行"},
        },
        "000001.SH": {
            "symbol": "000001.SH",
            "last_price": 3000,
            "prev_close": 3000,
            "volume": 1e8,
            "amount": 1e10,
            "ext": {"name": "上证"},
        },
        "399001.SZ": {
            "symbol": "399001.SZ",
            "last_price": 10000,
            "prev_close": 10000,
            "volume": 1e8,
            "amount": 1e10,
            "ext": {"name": "深证"},
        },
    }

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return plunge_quotes

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    args = SimpleNamespace(
        ignore_hours=True,
        dry_run=False,
        trace=None,
        holding=["600000.SH:10.0:8.0"],
        user_id="ou_test",
        force_send=False,
        urgent_only=False,
    )

    monkeypatch.setattr(radar, "send_feishu_tiered_alert", lambda *a, **kw: None)
    radar.run_once(args)
    after_fail = json.loads(state_file.read_text(encoding="utf-8"))
    assert after_fail["quotes"]["600000.SH"]["pct"] == 0.0
    assert after_fail["ts"] == seed_ts
    assert after_fail["cooldowns"] == {}

    sent: list[dict[str, object]] = []

    def capture(title, content, level="L1", **kw):
        sent.append({"level": level, "has_plunge": "持仓急跌" in content})
        return "om_retry"

    monkeypatch.setattr(radar, "send_feishu_tiered_alert", capture)
    radar.run_once(args)
    assert sent and sent[0]["level"] == "L3" and sent[0]["has_plunge"] is True
    after_ok = json.loads(state_file.read_text(encoding="utf-8"))
    assert "L3:holding_plunge:600000.SH" in after_ok["cooldowns"]
    assert after_ok["quotes"]["600000.SH"]["pct"] == -5.0


def test_daemon_loop_sends_l1_alert_on_third_consecutive_error(monkeypatch):
    radar = _load_radar_module()
    sent_alerts = []
    calls = 0

    def failing_run_once(args):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise RuntimeError("TickFlow timeout")
        raise KeyboardInterrupt()

    monkeypatch.setattr(radar, "run_once", failing_run_once)
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a, **kw: "ou_test")
    monkeypatch.setattr(
        radar,
        "send_feishu_tiered_alert",
        lambda title, content, **kw: sent_alerts.append((title, content, kw.get("level"))) or "om_err",
    )
    monkeypatch.setattr(radar.time, "sleep", lambda s: None)

    args = SimpleNamespace(interval=30, dry_run=False, user_id="ou_test")
    try:
        radar._run_daemon_loop(args)
    except KeyboardInterrupt:
        pass
    assert len(sent_alerts) == 1
    assert "连续 3 次扫描异常" in sent_alerts[0][0]
    assert sent_alerts[0][2] == "L1"


def test_missing_trace_shows_explicit_warning_banner_and_auto_fetches(tmp_path, monkeypatch, capsys):
    radar = _load_radar_module()
    today = cn_today()
    today_compact = today.strftime("%Y%m%d")

    # 1) When l2_map is empty, card body starts with explicit missing-trace warning
    _, _, body_no_trace, _, _ = radar.analyze_snapshot_diff({}, {}, {}, {}, now_ts=1000.0)
    assert "⚠️ **今日无威科夫 trace，L3/L4/板块共振已停用" in body_no_trace

    # When l2_map is present, warning is absent
    _, _, body_with_trace, _, _ = radar.analyze_snapshot_diff(
        {}, {}, {"688001": {"l2_eligible": True}}, {}, now_ts=1000.0
    )
    assert "今日无威科夫 trace" not in body_with_trace

    # 2) When trace dir is empty and auto_fetch=False, prints warning to stderr
    empty_dir = tmp_path / "empty_traces"
    empty_dir.mkdir()
    assert radar.load_wyckoff_l2_map(trace_dirs=[str(empty_dir)], auto_fetch=False) == {}
    assert "No review_trace_*.json.gz found" in capsys.readouterr().err

    # 3) When auto_fetch=True, calls gh run list + gh run download to populate trace_dir
    fetch_dir = tmp_path / "gh_traces"

    def fake_gh_run(cmd, **kw):
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"databaseId": 999888}]), stderr="")
        if "download" in cmd:
            art_dir = fetch_dir / "daily-job-artifacts-1" / "artifacts"
            art_dir.mkdir(parents=True, exist_ok=True)
            tf = art_dir / f"review_trace_{today_compact}.json.gz"
            with gzip.open(tf, "wt", encoding="utf-8") as f:
                json.dump(
                    {
                        "trade_date": today.isoformat(),
                        "symbols": {"688001": {"name": "自动拉取芯", "sector": "半导体", "l2_eligible": True}},
                    },
                    f,
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(radar.subprocess, "run", fake_gh_run)
    l2_loaded = radar.load_wyckoff_l2_map(trace_dirs=[str(fetch_dir)], auto_fetch=True)
    assert l2_loaded["688001"]["name"] == "自动拉取芯"


def test_breadth_alignment_coverage_gate_and_index_gap(tmp_path):
    radar = _load_radar_module()
    today = cn_today().isoformat()
    t_dir = tmp_path / "turnover"

    # Reproduction from review: prev frame has 500 red stocks, current frame only returns 99 unchanged
    prev_quotes = {f"{600001 + i:06d}.SH": {"pct": 1.5, "amt_yi": 0.5} for i in range(500)}
    prev_state = {"date": today, "ts": 1000.0, "red": 500, "cooldowns": {}, "quotes": prev_quotes}
    curr_99 = {
        f"{600001 + i:06d}.SH": {
            "symbol": f"{600001 + i:06d}.SH",
            "last_price": 10.15,
            "prev_close": 10.0,
            "volume": 5e6,
            "amount": 5e7,
            "ext": {"name": f"股票{i}"},
        }
        for i in range(99)
    }
    # Only 000001.SH present (399001.SZ missing) -> index gap warning
    curr_99["000001.SH"] = {
        "symbol": "000001.SH",
        "last_price": 3400.0,
        "prev_close": 3350.0,
        "volume": 1e9,
        "amount": 5000e8,
        "ext": {"name": "上证指数"},
    }
    level_gap, _, body_gap, red_gap, _ = radar.analyze_snapshot_diff(
        curr_99, prev_state, {"600001": {"l2_eligible": True}}, {}, now_ts=1200.0, turnover_dir=t_dir
    )
    assert red_gap == 99
    assert level_gap == "L1"
    assert "市场情绪5m骤变" not in body_gap
    assert "数据缺口：本帧只回来 99 只 A 股" in body_gap
    assert "指数数据缺口" in body_gap

    # When 480 out of 500 return (96% >= 90%) with unchanged prices, common-pool d_red must be 0 (not -20)
    curr_480 = {
        f"{600001 + i:06d}.SH": {
            "symbol": f"{600001 + i:06d}.SH",
            "last_price": 10.15,
            "prev_close": 10.0,
            "volume": 5e6,
            "amount": 5e7,
            "ext": {"name": f"股票{i}"},
        }
        for i in range(480)
    }
    level_ok, _, body_ok, _, _ = radar.analyze_snapshot_diff(
        curr_480, prev_state, {"600001": {"l2_eligible": True}}, {}, now_ts=1200.0, turnover_dir=t_dir
    )
    assert level_ok == "L1"
    assert "同池5m变化 +0 家" in body_ok


def test_intraday_turnover_curve_and_same_timestamp_comparison(tmp_path):
    radar = _load_radar_module()
    t_dir = tmp_path / "turnover"
    d1 = date(2026, 11, 4)
    d2 = date(2026, 11, 5)

    # Day 1 morning close (11:30) and afternoon (14:45): no previous day -> reports "无基准" and afternoon increment
    s1_am = radar.update_and_compare_turnover(d1, "11:30", 12000.0, index_ok=True, turnover_dir=t_dir)
    assert "同时刻对比：无基准" in s1_am
    s1_pm = radar.update_and_compare_turnover(d1, "14:45", 18000.0, index_ok=True, turnover_dir=t_dir)
    assert "同时刻对比：无基准" in s1_pm
    assert "13:00至今 6000亿" in s1_pm

    # Day 2 at 14:45: compares against Day 1 14:45 (18000亿) with explicit benchmark date 2026-11-04 -> +10.0%
    s2_pm = radar.update_and_compare_turnover(d2, "14:45", 19800.0, index_ok=True, turnover_dir=t_dir)
    assert "较 2026-11-04 14:45 18000亿 +10.0%" in s2_pm


def test_springboard_l4_cooldown_does_not_mute_holding_stop_phone():
    """Same-symbol springboard L4 must not consume the 86400s holding-stop phone budget."""
    radar = _load_radar_module()
    today = cn_today().isoformat()
    holdings_wide_stop = {"600000.SH": {"cost": 10.0, "stop": 8.0}}
    l2_map = {
        "600000": {
            "name": "浦发银行",
            "sector": "银行",
            "rps": 50.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "is_springboard": True,
        }
    }
    prev_quotes = {"600000.SH": {"pct": 1.0, "amt_yi": 0.2}}
    curr = {
        "600000.SH": {
            "symbol": "600000.SH",
            "last_price": 10.40,
            "prev_close": 10.0,
            "volume": 5e6,
            "amount": 10.4 * 5e6,
            "ext": {"name": "浦发银行"},
        }
    }
    for i in range(1, 101):
        sym = f"601{i:03d}.SH"
        prev_quotes[sym] = {"pct": 0.0, "amt_yi": 1.0}
        curr[sym] = {
            "symbol": sym,
            "last_price": 10.5 if i <= 85 else 9.5,
            "prev_close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "ext": {"name": f"s{i}"},
        }
    prev_state = {"date": today, "ts": 1000.0, "red": 50, "cooldowns": {}, "quotes": prev_quotes}
    level1, _, body1, _, cd1 = radar.analyze_snapshot_diff(curr, prev_state, l2_map, holdings_wide_stop, now_ts=1300.0)
    assert level1 == "L4"
    assert "起跳板" in body1
    assert "L4:springboard:600000.SH" in cd1
    assert "L4:holding_stop:600000.SH" not in cd1

    compact = {}
    for sym, qq in curr.items():
        m = radar.compute_quote_metrics(qq)
        if m:
            compact[sym] = {"pct": round(m["pct"], 3), "amt_yi": round(m["amt_yi"], 3)}
    curr2 = dict(curr)
    curr2["600000.SH"] = {
        "symbol": "600000.SH",
        "last_price": 9.40,
        "prev_close": 10.0,
        "volume": 5e6,
        "amount": 9.40 * 5e6,
        "ext": {"name": "浦发银行"},
    }
    prev_state2 = {"date": today, "ts": 1300.0, "red": 85, "cooldowns": dict(cd1), "quotes": compact}
    holdings_hit = {"600000.SH": {"cost": 10.0, "stop": 9.50}}
    level2, _, body2, _, cd2 = radar.analyze_snapshot_diff(curr2, prev_state2, l2_map, holdings_hit, now_ts=4900.0)
    assert level2 == "L4"
    assert "破位止损" in body2
    assert "L4:holding_stop:600000.SH" in cd2


def test_urgent_phone_failure_does_not_return_msg_id(monkeypatch):
    radar = _load_radar_module()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "+messages-send" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"code": 0, "data": {"message_id": "om_card_ok"}}),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="urgent_phone denied")

    monkeypatch.setattr(radar.subprocess, "run", fake_run)
    assert (
        radar.send_feishu_tiered_alert("Title", "Body", level="L4", user_open_id="ou_mock_user", dry_run=False) is None
    )
    assert any("urgent_phone" in c for c in calls)


def test_dry_run_does_not_write_shared_state_file(tmp_path, monkeypatch):
    radar = _load_radar_module()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"date": "2099-01-01", "cooldowns": {}, "quotes": {}}), encoding="utf-8")
    before = state_file.read_text(encoding="utf-8")
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {})

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return {
                "600000.SH": {
                    "symbol": "600000.SH",
                    "last_price": 9.40,
                    "prev_close": 10.0,
                    "volume": 1000,
                    "amount": 9400.0,
                    "ext": {"name": "浦发银行"},
                }
            }

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    args = SimpleNamespace(
        ignore_hours=True,
        dry_run=True,
        trace=None,
        holding=["600000.SH:10.0:9.50"],
        user_id="ou_test",
        force_send=False,
        urgent_only=False,
    )
    radar.run_once(args)
    assert state_file.read_text(encoding="utf-8") == before
