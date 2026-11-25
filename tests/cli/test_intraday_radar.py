"""Unit tests for the full-market 5-minute intraday radar script."""

from __future__ import annotations

import gzip
import importlib.util
import json
from datetime import date, datetime, timedelta
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
    payload0 = json.loads(calls[0][calls[0].index("--content") + 1])
    assert payload0["header"]["title"]["content"] == "【来自日内主线雷达】Title"

    radar.send_feishu_tiered_alert(
        "【来自日内主线雷达】已含前缀", "Body", level="L1", user_open_id="ou_mock_user", dry_run=False
    )
    payload_idem = json.loads(calls[2][calls[2].index("--content") + 1])
    assert payload_idem["header"]["title"]["content"] == "【来自日内主线雷达】已含前缀"

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
        "high": 10.75,
        "low": 10.10,
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


def test_holding_quote_gap_cooldown_is_per_symbol():
    """A gap stamp for holding A must not mute a later gap urgent for holding B."""
    radar = _load_radar_module()
    today = cn_today().isoformat()
    holdings = {
        "600000.SH": {"cost": 10.0, "stop": 9.50},
        "000001.SZ": {"cost": 10.0, "stop": 9.50},
    }
    base_quotes = {
        "600001.SH": {
            "symbol": "600001.SH",
            "last_price": 10.1,
            "prev_close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "ext": {"name": "其他"},
        },
        "000001.SZ": {
            "symbol": "000001.SZ",
            "last_price": 10.1,
            "prev_close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "ext": {"name": "平安银行"},
        },
    }
    prev_state = {
        "date": today,
        "ts": 1000.0,
        "quotes": {
            "600000.SH": {"pct": -1.0, "amt_yi": 0.5},
            "000001.SZ": {"pct": -1.0, "amt_yi": 0.5},
            "600001.SH": {"pct": 1.0, "amt_yi": 0.5},
        },
        "cooldowns": {},
    }
    level_a, _, body_a, _, cds = radar.analyze_snapshot_diff(base_quotes, prev_state, {}, holdings, now_ts=1200.0)
    assert level_a == "L2"
    assert "600000.SH" in body_a
    assert cds.get("L2:holding_quote_gap:600000.SH") == 1200.0
    assert "L2:holding_quote_gap" not in cds

    quotes_b = {
        "600001.SH": base_quotes["600001.SH"],
        "600000.SH": {
            "symbol": "600000.SH",
            "last_price": 10.0,
            "prev_close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "ext": {"name": "浦发银行"},
        },
    }
    prev_b = {**prev_state, "ts": 1200.0, "cooldowns": cds}
    level_b, _, body_b, _, cds_b = radar.analyze_snapshot_diff(quotes_b, prev_b, {}, holdings, now_ts=1500.0)
    assert level_b == "L2"
    assert "000001.SZ" in body_b
    assert "【持仓行情缺口】" in body_b
    assert cds_b.get("L2:holding_quote_gap:000001.SZ") == 1500.0
    assert not radar.should_send_card("L1", 1500.0, 1400.0, False, False)
    assert radar.should_send_card(level_b, 1500.0, 1400.0, False, True)


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
            "002594": {
                "name": "起跳测试深市",
                "sector": "汽车",
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
        json.dumps({"recommend_date": int(today_compact), "rows": [{"code": 2594, "springboard_combo": "A+B+C"}]}),
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
    assert l2_map["002594"]["is_springboard"] is True
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
    today_d = cn_today()
    today = today_d.isoformat()
    alert_log = tmp_path / "radar_alerts.jsonl"
    t_dir = tmp_path / "turnover"
    ts_0940 = datetime(today_d.year, today_d.month, today_d.day, 9, 40, tzinfo=CN_TZ).timestamp()
    ts_1035 = datetime(today_d.year, today_d.month, today_d.day, 10, 35, tzinfo=CN_TZ).timestamp()
    prev_state = {
        "date": today,
        "ts": ts_0940 - 300.0,
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
    # 1) Pre-10:30 (09:40 BJT): downgraded to L1 card observation, no L3 SMS, does NOT burn L3 cooldown
    level_am, _, body_am, _, cd_am = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, {}, now_ts=ts_0940, turnover_dir=t_dir, alert_log_path=alert_log
    )
    assert level_am == "L1"
    assert "待10:30验钞观察" in body_am
    assert "L1:pre1030_obs:688001.SH" in cd_am
    assert "L3:wyckoff_ignition:688001.SH" not in cd_am
    assert not alert_log.exists()

    # 2) Post-10:30 (10:35 BJT): equity 5m pulses stay L1 obs (never send L3 chase SMS!) and never touch L3 alert_log
    prev_state["cooldowns"] = cd_am
    prev_state["ts"] = ts_1035 - 300.0
    level1, _, body1, _, cd1 = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, {}, now_ts=ts_1035, turnover_dir=t_dir, alert_log_path=alert_log
    )
    assert level1 == "L1"
    assert "688001.SH" in body1
    assert "👀[盘中脉冲仅观察·禁追高点" in body1
    assert "L1:pulse_obs:688001.SH" in cd1
    assert not alert_log.exists()

    prev_state["cooldowns"] = cd1
    prev_state["ts"] = ts_1035
    level2, _, _, _, _ = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, {}, now_ts=ts_1035 + 200.0, turnover_dir=t_dir
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

    # Midday break (11:54 -> 11:30) clamping: compares against Day 1 11:30 (12000亿) instead of "无基准"
    s2_midday = radar.update_and_compare_turnover(d2, "11:54", 11400.0, index_ok=True, turnover_dir=t_dir)
    assert "较 2026-11-04 11:30 12000亿 -5.0%" in s2_midday


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


def test_telegraph_catalyst_theme_and_wyckoff_mapping() -> None:
    spec = importlib.util.spec_from_file_location("telegraph_catalysts", SCRIPT_PATH.parent / "telegraph_catalysts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    l2_map = {
        "000700": {
            "name": "模塑科技",
            "sector": "汽车配件",
            "rps": 93.8,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["主升通道"],
        },
        "688316": {
            "name": "青云科技",
            "sector": "软件服务",
            "rps": 14.9,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": False,
            "channels": ["地量蓄势"],
        },
    }
    quotes = {"000700.SZ": {"last_price": 9.92, "prev_close": 9.87}}
    theme, picks = mod.match_theme_and_candidates("【丰田汽车将在重组中设立机器人部门】", l2_map, quotes)
    assert "机器人" in theme
    assert any("000700" in p and "模塑科技" in p for p in picks)

    noise_theme, noise_picks = mod.match_theme_and_candidates("荷兰皇家航空发言人：A320订单尚未取消", l2_map, quotes)
    assert noise_theme == ""
    assert noise_picks == []

    etf_quotes = {
        "588000.SH": {"amount": 188.86e8},
        "159915.SZ": {"amount": 163.95e8},
        "510210.SH": {"amount": 9.97e8},
    }
    prev_etf = {
        "588000.SH": {"amt_yi": 170.0},
        "159915.SZ": {"amt_yi": 150.0},
        "510210.SH": {"amt_yi": 9.5},
    }
    nt_dt = datetime(2026, 11, 6, 10, 30, tzinfo=CN_TZ)
    nt_summary, nt_l2 = mod.summarize_national_team_etfs(etf_quotes, prev_etf, has_baseline=True, now_dt=nt_dt)
    assert "362.8亿" in nt_summary
    assert "588000" in nt_summary
    assert "国家队20大宽基ETF 5m放量异动" in nt_l2
    open_dt = datetime(2026, 11, 6, 9, 35, tzinfo=CN_TZ)
    _, open_l2 = mod.summarize_national_team_etfs(etf_quotes, prev_etf, has_baseline=True, now_dt=open_dt)
    assert open_l2 == ""

    ag_map = {
        f"6003{i:02d}": {
            "name": f"种业股{i}",
            "sector": "种植业" if i != 1 else "渔业",
            "rps": 20.0 + i,
            "l2_eligible": i == 5,
            "is_candidate": False,
        }
        for i in range(10)
    }
    ag_quotes = {f"6003{i:02d}.SH": {"pct": 3.5} for i in range(10)}
    ag_theme, ag_picks = mod.match_theme_and_candidates("农业农村部推进种业振兴与粮食安全", ag_map, ag_quotes)
    assert "农业" in ag_theme
    assert any("600305" in p for p in ag_picks)
    mainline_md = mod.summarize_top_sector_mainlines(ag_quotes, ag_map)
    assert "种植业" in mainline_md
    assert "600305" in mainline_md
    assert "字头抱团" not in mainline_md


def test_review_fixes_telegraph_filtering_cooldown_and_sector_priority(tmp_path) -> None:
    radar = _load_radar_module()
    spec = importlib.util.spec_from_file_location("telegraph_catalysts", SCRIPT_PATH.parent / "telegraph_catalysts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1. 159919.SZ suffix & BJ 92xxxx symbol resolution
    assert "159919.SZ" in mod.NATIONAL_TEAM_20_ETFS
    assert "159919.SH" not in mod.NATIONAL_TEAM_20_ETFS
    assert mod.resolve_code6_symbol("920087") == "920087.BJ"

    # 2. Foreign & negative headlines must not produce false Wyckoff buy mappings; macro news has empty picks
    l2_map = {
        "688316": {"name": "青云科技", "sector": "软件服务", "rps": 25.0, "l2_eligible": True},
        "920087": {"name": "秋乐种业", "sector": "种植业", "rps": 30.0, "l2_eligible": True},
    }
    for bad_txt in (
        "印度汽车经销商协会：10月汽车零售销量同比增长",
        "午评：创业板指收跌3.5%，算力硬件领跌",
        "日本篡改核电站数据公司被勒令整改",
        "韩财政部召开半导体专家座谈会",
        "欧盟能源监管：欧洲电网费用或翻倍",
    ):
        assert mod.match_theme_and_candidates(bad_txt, l2_map) == ("", [])

    stat_theme, stat_picks = mod.match_theme_and_candidates("ETF两市成交额报6372亿，半导体ETF领跑", l2_map)
    assert stat_picks == []
    anti_dump_theme, _ = mod.match_theme_and_candidates("商务部对欧盟进口对位芳纶发起反倾销立案调查", l2_map)
    assert "化工" in anti_dump_theme

    macro_theme, macro_picks = mod.match_theme_and_candidates("央行发布最新再贷款操作指引", l2_map)
    assert "宏观" in macro_theme
    assert macro_picks == []

    # 3. Cooldown deduplication prevents identical telegraph item from repeating across consecutive 5m scans
    cd: dict[str, float] = {}
    fake_raw = [{"id": "sina_101", "ts": 1950.0, "hm": "13:15", "text": "工信部加快推进具身智能机器人产业落地"}]
    first_lines = mod.scan_live_telegraph_catalysts(l2_map, cooldowns=cd, now_ts=2000.0, raw_items=fake_raw)
    second_lines = mod.scan_live_telegraph_catalysts(l2_map, cooldowns=cd, now_ts=2300.0, raw_items=fake_raw)
    assert len(first_lines) == 1
    assert second_lines == []

    # 4. National Team ETF 5m surge >=25亿 after 09:45 promotes card level to L2 and sector resonance survives >6 stock L2s
    today = cn_today().isoformat()
    post_open_ts = datetime(2026, 11, 6, 10, 30, tzinfo=CN_TZ).timestamp()
    prev_q = {"588000.SH": {"pct": -1.0, "amt_yi": 100.0}, "159919.SZ": {"pct": -0.5, "amt_yi": 20.0}}
    curr_q: dict[str, dict] = {
        "588000.SH": {"symbol": "588000.SH", "last_price": 1.40, "prev_close": 1.41, "volume": 1e9, "amount": 130.0e8},
        "159919.SZ": {"symbol": "159919.SZ", "last_price": 4.00, "prev_close": 4.02, "volume": 1e8, "amount": 25.0e8},
    }
    sec_l2_map: dict[str, dict] = {}
    for i in range(10):
        code = f"6001{i:02d}"
        sym = f"{code}.SH"
        is_ag = i < 3
        sec_l2_map[code] = {
            "name": f"种业{i}" if is_ag else f"其他{i}",
            "sector": "种植业" if is_ag else f"行业{i}",
            "rps": 60.0,
            "l2_eligible": True,
            "channels": ["主升通道"],
        }
        prev_q[sym] = {"pct": 1.0, "amt_yi": 1.0}
        curr_q[sym] = {
            "symbol": sym,
            "last_price": 10.45,
            "prev_close": 10.0,
            "volume": 2e7,
            "amount": 1.8e8,
            "ext": {"name": sec_l2_map[code]["name"]},
        }
    prev_state = {"date": today, "ts": post_open_ts - 300.0, "red": 10, "cooldowns": {}, "quotes": prev_q}
    level, title_l2, body, _, _ = radar.analyze_snapshot_diff(
        curr_q, prev_state, sec_l2_map, {}, now_ts=post_open_ts, turnover_dir=tmp_path / "to"
    )
    assert level == "L2"
    assert "【L2 加急焦点】" in title_l2
    assert "🚨 **【本次 L2 加急核心焦点 — 为什么给你发加急】**" in body
    assert "💡 **关注重点**" in body
    assert "国家队20大宽基ETF 5m放量异动" in body
    assert "种植业 板块5m共振" in body


def test_tariff_telegraph_zero_bias_l2_leaders_and_upper_wick_filter(tmp_path) -> None:
    radar = _load_radar_module()
    spec = importlib.util.spec_from_file_location("telegraph_catalysts", SCRIPT_PATH.parent / "telegraph_catalysts.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    l2_map: dict[str, dict] = {
        "601898": {
            "name": "中煤能源",
            "sector": "煤炭开采",
            "rps": 44.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["吸筹通道+地量蓄势"],
        },
        "002088": {
            "name": "鲁阳节能",
            "sector": "煤炭开采",
            "rps": 96.4,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": False,
            "channels": ["点火破局"],
        },
        "600261": {
            "name": "阳光照明",
            "sector": "家用电器",
            "rps": 99.2,
            "l2_eligible": False,
            "l3_eligible": False,
            "is_candidate": False,
            "channels": [],
        },
        "002611": {
            "name": "东方精工",
            "sector": "专用机械",
            "rps": 34.5,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
        },
    }
    tariff_txt = "经国务院批准，自2026年11月10日13时01分起，在2个月内继续暂停实施24%的对美加征关税税率，保留10%的对美加征关税税率"
    t_theme, t_picks = mod.match_theme_and_candidates(tariff_txt, l2_map)
    assert "关税豁免" in t_theme
    assert not any("600261" in p for p in t_picks)

    quotes: dict[str, dict] = {}
    for i in range(8):
        code = "601898" if i == 0 else ("002088" if i == 1 else f"6016{i + 10:02d}")
        sym = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        l2_map.setdefault(
            code,
            {"name": f"煤企{i}", "sector": "煤炭开采", "rps": 35.0, "l2_eligible": False, "l3_eligible": False},
        )
        quotes[sym] = {
            "symbol": sym,
            "last_price": 13.38 if i == 0 else (11.94 if i == 1 else 10.20),
            "prev_close": 12.91 if i == 0 else (11.09 if i == 1 else 10.00),
            "open": 13.00 if i == 0 else (10.28 if i == 1 else 10.00),
            "high": 13.45 if i == 0 else (12.19 if i == 1 else 10.25),
            "low": 12.95 if i == 0 else (10.27 if i == 1 else 9.98),
            "volume": 4.0e7 if i == 0 else 2.2e7,
            "amount": 5.30e8 if i == 0 else 1.74e8,
        }
    zb_md = mod.summarize_zero_bias_l2_leaders(quotes, l2_map)
    assert "全市场 L2+L3 稳健中军盘中观察池" in zb_md
    assert "601898.SH 中煤能源" in zb_md
    assert "002088" not in zb_md

    # Upper-wick spike (>2.0%) is rejected by _check_wyckoff_signals, and non-L2 / high-RPS surge is tagged [⚠️妖股/高位勿追]
    today = cn_today().isoformat()
    ts = datetime(2026, 11, 9, 10, 35, tzinfo=CN_TZ).timestamp()
    quotes["002611.SZ"] = {
        "symbol": "002611.SZ",
        "last_price": 14.35,
        "prev_close": 13.91,
        "open": 14.10,
        "high": 14.86,
        "low": 14.10,
        "volume": 5e7,
        "amount": 7.15e8,
        "ext": {"name": "东方精工"},
    }
    quotes["600261.SH"] = {
        "symbol": "600261.SH",
        "last_price": 5.74,
        "prev_close": 5.40,
        "open": 5.32,
        "high": 5.80,
        "low": 5.09,
        "volume": 3e8,
        "amount": 16.8e8,
        "ext": {"name": "阳光照明"},
    }
    quotes["000001.SH"] = {
        "symbol": "000001.SH",
        "last_price": 3662.81,
        "prev_close": 3660.75,
        "open": 3682.04,
        "high": 3683.73,
        "low": 3662.25,
        "volume": 2e10,
        "amount": 4281.23e8,
        "ext": {"name": "上证指数"},
    }
    prev_state = {
        "date": today,
        "ts": ts - 300.0,
        "red": 8,
        "cooldowns": {},
        "quotes": {"002611.SZ": {"pct": 1.0, "amt_yi": 5.0}, "600261.SH": {"pct": 2.0, "amt_yi": 10.0}},
    }
    holdings = {"601898.SH": {"cost": 13.40, "stop": 12.80}}
    _, _, body, _, _ = radar.analyze_snapshot_diff(
        quotes, prev_state, l2_map, holdings, now_ts=ts, turnover_dir=tmp_path / "to2"
    )
    assert "高开回落防骗炮" in body
    assert "成本 13.40 (-0.15%)" in body
    assert "🟢均线上" in body
    assert "600261.SH 阳光照明** [⚠️妖股/高位勿追]" in body
    assert "002611.SZ 东方精工** [专用机械]" not in body

    # Post-10:30 steady leader push stays L1 obs with 🎯稳健中军加速 tag (L3 SMS strictly reserved for holding plunges)
    l2_map["002635"] = {
        "name": "安洁科技",
        "sector": "煤炭开采",
        "rps": 40.0,
        "l2_eligible": True,
        "l3_eligible": True,
        "is_candidate": False,
        "channels": ["吸筹通道"],
    }
    prev_state["quotes"]["002635.SZ"] = {"pct": 2.10, "amt_yi": 2.00}
    quotes["002635.SZ"] = {
        "symbol": "002635.SZ",
        "last_price": 14.38,
        "prev_close": 13.90,
        "open": 14.05,
        "high": 14.43,
        "low": 13.88,
        "volume": 1.65e7,
        "amount": 2.35e8,
        "ext": {"name": "安洁科技"},
    }
    level_steady, _, body_steady, _, cd_steady = radar.analyze_snapshot_diff(
        quotes, prev_state, l2_map, holdings, now_ts=ts, turnover_dir=tmp_path / "to3"
    )
    assert level_steady == "L1"
    assert "002635.SZ 安洁科技" in body_steady
    assert "🎯稳健中军加速" in body_steady
    assert "👀[盘中脉冲仅观察·禁追高点" in body_steady
    assert "L3:wyckoff_ignition:002635.SZ" not in cd_steady


def test_sh_index_never_collides_with_sz_equity_and_median_thermometer_renders(tmp_path):
    radar = _load_radar_module()
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    quotes = {
        "000682.SH": {
            "symbol": "000682.SH",
            "last_price": 2011.0,
            "prev_close": 1980.0,
            "open": 1985.0,
            "high": 2020.0,
            "low": 1980.0,
            "volume": 1e9,
            "amount": 550e8,
            "ext": {"name": "科创信息"},
        },
        "000682.SZ": {
            "symbol": "000682.SZ",
            "last_price": 10.15,
            "prev_close": 10.00,
            "open": 10.02,
            "high": 10.20,
            "low": 9.98,
            "volume": 2e7,
            "amount": 2.02e8,
            "ext": {"name": "东方电子"},
        },
        "399006.SZ": {
            "symbol": "399006.SZ",
            "last_price": 2577.75,
            "prev_close": 2500.00,
            "volume": 1e9,
            "amount": 4800e8,
            "ext": {"name": "创业板指"},
        },
    }
    assert tc.resolve_code6_symbol("000682", quotes) == "000682.SZ"
    assert not tc._is_equity_symbol("000682.SH")
    assert tc._is_equity_symbol("000682.SZ")

    l2_map = {}
    for idx in range(8):
        code6 = f"00068{idx}"
        l2_map[code6] = {
            "name": f"深市电企{idx}",
            "sector": "电气设备",
            "rps": 35.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": False,
            "channels": ["地量蓄势"],
        }
        # Populate only .SH indices for 0..6 (must NOT be counted as Shenzhen stocks!)
        if idx != 2:
            quotes[f"{code6}.SH"] = {
                "symbol": f"{code6}.SH",
                "last_price": 3090.0,
                "prev_close": 3000.0,
                "open": 3010.0,
                "high": 3095.0,
                "low": 3005.0,
                "volume": 1e8,
                "amount": 50e8,
            }
    assert tc.summarize_top_sector_mainlines(quotes, l2_map) == ""
    assert tc.summarize_zero_bias_l2_leaders(quotes, l2_map) == ""

    _, _, body, _, _ = radar.analyze_snapshot_diff(quotes, {}, l2_map, {}, now_ts=1000.0, turnover_dir=tmp_path / "to")
    assert "创业板 +3.11%（全A中位数 +1.50%）" in body


def test_silent_l1_scan_preserves_telegraph_until_next_sent_card(tmp_path, monkeypatch):
    radar = _load_radar_module()
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    today_str = radar.cn_today().isoformat()
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"date": today_str, "ts": 900.0, "last_l1_ts": 900.0, "cooldowns": {}, "quotes": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {})
    fake_item = [{"id": "sina_tg_950", "ts": 950.0, "hm": "09:31", "text": "工信部加快推进具身智能机器人产业落地"}]
    monkeypatch.setattr(tc, "fetch_sina_telegraph", lambda pages=1: list(fake_item))
    monkeypatch.setattr(tc, "fetch_wscn_telegraph", lambda: [])

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return {"000001.SH": {"last_price": 3300.0, "prev_close": 3300.0, "amount": 100e8}}

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    sent_cards: list[str] = []
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a: "ou_mock")
    monkeypatch.setattr(
        radar, "send_feishu_tiered_alert", lambda title, content, **kw: sent_cards.append(content) or "om_ok"
    )
    args = SimpleNamespace(
        ignore_hours=True, dry_run=False, trace=None, holding=[], user_id="ou_mock", force_send=False, urgent_only=False
    )

    monkeypatch.setattr(radar.time, "time", lambda: 1200.0)
    radar.run_once(args)
    assert sent_cards == []
    assert "tg:sina_tg_950" not in json.loads(state_file.read_text(encoding="utf-8"))["cooldowns"]

    monkeypatch.setattr(radar.time, "time", lambda: 2700.0)
    radar.run_once(args)
    assert len(sent_cards) == 1
    assert "具身智能" in sent_cards[0]
    assert "tg:sina_tg_950" in json.loads(state_file.read_text(encoding="utf-8"))["cooldowns"]


def test_heartbeat_mode_sends_compact_card_without_resetting_l1_or_telegraph_cooldown(tmp_path, monkeypatch):
    radar = _load_radar_module()
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    today_str = radar.cn_today().isoformat()
    state_file = tmp_path / "state_hb.json"
    state_file.write_text(
        json.dumps({"date": today_str, "ts": 1000.0, "last_l1_ts": 1000.0, "cooldowns": {}, "quotes": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", tmp_path / "alerts_hb.jsonl")
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover_hb")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {})
    fake_item = [{"id": "sina_tg_hb1", "ts": 1050.0, "hm": "13:05", "text": "央行推进互换便利工具落地支持资本市场"}]
    monkeypatch.setattr(tc, "fetch_sina_telegraph", lambda pages=1: list(fake_item))
    monkeypatch.setattr(tc, "fetch_wscn_telegraph", lambda: [])

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return {
                "000001.SH": {"last_price": 3306.6, "prev_close": 3300.0, "volume": 1e9, "amount": 5000e8},
                "399001.SZ": {"last_price": 10521.0, "prev_close": 10500.0, "volume": 1e9, "amount": 6000e8},
                "399006.SZ": {"last_price": 2102.1, "prev_close": 2100.0, "volume": 5e8, "amount": 2500e8},
                "600900.SH": {"last_price": 28.14, "prev_close": 28.00, "volume": 1e7, "amount": 2.8e8},
            }

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    sent_events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a: "ou_mock")
    monkeypatch.setattr(
        radar,
        "send_feishu_tiered_alert",
        lambda title, content, level="L1", **kw: sent_events.append((level, title, content)) or "om_hb",
    )
    args = SimpleNamespace(
        ignore_hours=True,
        dry_run=False,
        trace=None,
        holding=["600900.SH:27.50:26.50"],
        user_id="ou_mock",
        force_send=False,
        urgent_only=False,
        heartbeat=True,
    )

    monkeypatch.setattr(radar.time, "time", lambda: 1300.0)
    radar.run_once(args)
    assert len(sent_events) == 1
    hb_level, hb_title, hb_body = sent_events[0]
    assert hb_level == "L1"
    assert hb_title.startswith("【💓 心跳】")
    assert "上证+0.20% 创+0.10%" in hb_title
    assert "🛡️ 持仓正常：600900 +0.50%" in hb_body
    assert len(hb_body.splitlines()) == 2
    saved_after_hb = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_after_hb["last_l1_ts"] == 1000.0
    assert "tg:sina_tg_hb1" not in saved_after_hb["cooldowns"]

    monkeypatch.setattr(radar.time, "time", lambda: 2800.0)
    radar.run_once(args)
    assert len(sent_events) == 2
    full_level, full_title, full_body = sent_events[1]
    assert full_level == "L1"
    assert full_title.startswith("【L1】")
    assert "5m全市场雷达" in full_title
    assert "互换便利" in full_body
    saved_after_full = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_after_full["last_l1_ts"] == 2800.0
    assert "tg:sina_tg_hb1" in saved_after_full["cooldowns"]

    # If a holding is below hard stop during its 24h L4 cooldown, heartbeat line must never say "持仓正常"
    _, breach_body = radar._format_heartbeat_card(
        {"600900.SH": {"last_price": 26.40, "prev_close": 28.00, "volume": 1e7, "amount": 2.64e8}},
        {"600900.SH": {"cost": 27.50, "stop": 26.50}},
        500,
        "14:05",
    )
    assert "⚠️ 持仓破位待处理：600900 -5.71%🔴破止损" in breach_body
    assert "持仓正常" not in breach_body


def test_multi_day_wyckoff_funnel_continuity_and_shakeout_recall(tmp_path):
    radar = _load_radar_module()
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    days = ["2026-11-10", "2026-11-11", "2026-11-12"]
    for idx, d_str in enumerate(days):
        ymd = d_str.replace("-", "")
        symbols = {
            # 1) 3-day consecutive candidate -> 🏆3日候选连入
            "600111.SH": {
                "name": "三日连入股",
                "sector": "半导体",
                "rps_fast": 35.0,
                "l2_eligible": True,
                "l3_eligible": True,
                "stage": "候选池已捕获",
                "l2_channels": ["地量蓄势"],
            },
            # 2) T-3 & T-2 candidate, shaken out on T-1 (idx==2) -> ♻️T-2洗盘回流
            "002222.SZ": {
                "name": "洗盘回流股",
                "sector": "专用机械",
                "rps_fast": 42.0,
                "l2_eligible": idx < 2,
                "l3_eligible": idx < 2,
                "stage": "候选池已捕获" if idx < 2 else "观察",
                "l2_channels": ["吸筹通道"] if idx < 2 else [],
            },
            # 3) Absent in T-3 & T-2, sudden 1-day impulse in T-1 (idx==2) -> ⚠️T-1单日新进 (demoted from L3)
            "300333.SZ": {
                "name": "一日游脉冲",
                "sector": "元器件",
                "rps_fast": 55.0,
                "l2_eligible": idx == 2,
                "l3_eligible": idx == 2,
                "stage": "观察",
                "l2_channels": ["地量蓄势"] if idx == 2 else [],
            },
        }
        with gzip.open(trace_dir / f"review_trace_{ymd}.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"trade_date": d_str, "symbols": symbols}, f)

    merged = radar.load_wyckoff_l2_map(trace_dirs=[str(trace_dir)], max_age_days=None, auto_fetch=False)
    assert merged["600111"]["streak_days"] == 3
    assert merged["600111"]["funnel_tag"] == "🏆3日候选连入"
    assert merged["600111"]["is_candidate"] is True

    assert merged["002222"]["t2_shakeout_recall"] is True
    assert merged["002222"]["is_candidate"] is True
    assert merged["002222"]["l2_eligible"] is True
    assert merged["002222"]["channels"] == ["吸筹通道"]
    assert merged["002222"]["funnel_tag"] == "♻️T-2洗盘回流"

    assert merged["300333"]["t1_isolated_spike"] is True
    assert merged["300333"]["l3_eligible"] is False
    assert merged["300333"]["is_candidate"] is False
    assert merged["300333"]["funnel_tag"] == "⚠️T-1单日新进"

    # Verify sub-sector L2 cluster (>=4 L2 stocks up >=+2%) allows 3-day consecutive leader (like 002452 长高电气)
    # to enter summarize_zero_bias_l2_leaders even when broad sector red_ratio < 75%
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    sec_l2_map = {}
    sec_quotes = {}
    for i in range(10):
        c6 = f"00245{i}"
        sym = f"{c6}.SZ"
        is_up = i < 4  # 4 L2 stocks up >= +2.0% out of 10 (red_ratio = 40% < 75%)
        sec_l2_map[c6] = {
            "name": "长高电气" if i == 2 else f"电企{i}",
            "sector": "电气设备",
            "rps": 32.0,
            "l2_eligible": True,
            "l3_eligible": i == 2,
            "is_candidate": False,
            "channels": ["吸筹通道", "地量蓄势"],
            "streak_days": 3 if i == 2 else 1,
            "funnel_tag": "🔥3日L2+L3连入" if i == 2 else "",
        }
        sec_quotes[sym] = {
            "symbol": sym,
            "last_price": 9.88 if i == 2 else (10.30 if is_up else 9.80),
            "prev_close": 9.45 if i == 2 else 10.00,
            "open": 9.50 if i == 2 else 10.00,
            "high": 9.90 if i == 2 else (10.35 if is_up else 10.00),
            "low": 9.48 if i == 2 else (10.00 if is_up else 9.75),
            "volume": 1.9e7,
            "amount": 1.85e8,
            "vwap": 9.78 if i == 2 else 10.10,
        }
    zb_summary = tc.summarize_zero_bias_l2_leaders(sec_quotes, sec_l2_map)
    assert "002452.SZ 长高电气" in zb_summary
    assert "🔥3日L2+L3连入" in zb_summary


def test_l2_only_streak_blocks_l1_trend_bypass_candidates(tmp_path) -> None:
    radar = _load_radar_module()
    trace_dir = tmp_path / "traces_l2_only"
    trace_dir.mkdir()
    for d_str in ("2026-11-12", "2026-11-13", "2026-11-16"):
        ymd = d_str.replace("-", "")
        symbols = {
            "300133.SZ": {
                "name": "旁路样本",
                "sector": "影视音像",
                "rps_fast": 48.0,
                "l2_eligible": False,
                "l3_eligible": False,
                "stage": "候选池已捕获",
                "l2_channels": [],
            }
        }
        with gzip.open(trace_dir / f"review_trace_{ymd}.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"trade_date": d_str, "symbols": symbols}, f)

    merged = radar.load_wyckoff_l2_map(trace_dirs=[str(trace_dir)], max_age_days=None, auto_fetch=False)
    assert merged["300133"]["streak_days"] == 0
    assert merged["300133"]["funnel_tag"] == ""
    assert merged["300133"].get("t2_shakeout_recall") is not True


def test_preopen_unrolled_snapshot_skips_stop_cooldown_and_state_write(tmp_path, monkeypatch) -> None:
    radar = _load_radar_module()
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    monkeypatch.setattr(tc, "fetch_sina_telegraph", lambda pages=1: [])
    monkeypatch.setattr(tc, "fetch_wscn_telegraph", lambda: [])

    today_d = cn_today()
    ts_0925 = datetime(today_d.year, today_d.month, today_d.day, 9, 25, 40, tzinfo=CN_TZ).timestamp()
    state_file = tmp_path / "state_preopen.json"
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", tmp_path / "alerts_preopen.jsonl")
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover_preopen")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {})

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return {
                "000001.SH": {"last_price": 3602.57, "prev_close": 3602.57, "amount": 4200e8},
                "399001.SZ": {"last_price": 10690.78, "prev_close": 10690.78, "amount": 4800e8},
                "603011.SH": {"last_price": 15.74, "prev_close": 15.74, "amount": 3e8},
            }

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    sent_cards: list[tuple[str, str, str]] = []
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a: "ou_mock")
    monkeypatch.setattr(
        radar,
        "send_feishu_tiered_alert",
        lambda title, content, level="L1", **kw: sent_cards.append((level, title, content)) or "om_preopen",
    )
    monkeypatch.setattr(radar.time, "time", lambda: ts_0925)
    args = SimpleNamespace(
        ignore_hours=True,
        dry_run=False,
        trace=None,
        holding=["603011.SH:18.252:17.55"],
        user_id="ou_mock",
        force_send=False,
        urgent_only=False,
        heartbeat=True,
    )
    radar.run_once(args)

    assert len(sent_cards) == 1
    assert sent_cards[0][0] == "L1"
    assert "盘前竞价待切换" in sent_cards[0][1]
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert "L4:holding_stop:603011.SH" not in saved["cooldowns"]
    assert saved["last_l1_ts"] == 0.0
    assert saved["quotes"] == {}


def test_afternoon_telegraph_anti_chase_and_nt_etf_floor_split() -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    l2_map = {
        "002261": {
            "name": "拓维信息",
            "sector": "软件服务",
            "rps": 45.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "streak_days": 3,
            "funnel_tag": "🔥3日L2+L3连入",
        },
        "300999": {
            "name": "高追样本",
            "sector": "软件服务",
            "rps": 55.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "streak_days": 3,
            "funnel_tag": "🔥3日L2+L3连入",
        },
        "688316": {
            "name": "温和样本",
            "sector": "软件服务",
            "rps": 42.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "streak_days": 2,
            "funnel_tag": "🔥2日L2+L3连入",
        },
    }
    quotes = {
        "002261.SZ": {"last_price": 10.22, "prev_close": 10.00, "amount": 2e8, "vwap": 10.10},
        "300999.SZ": {"last_price": 10.45, "prev_close": 10.00, "amount": 2e8, "vwap": 10.20},
        "688316.SH": {"last_price": 10.35, "prev_close": 10.00, "amount": 2e8, "vwap": 10.20},
    }
    raw_items = [{"id": "sina_pm", "ts": 1000.0, "hm": "12:50", "text": "工信部加快推进国产算力芯片与软件协同突破"}]
    lines = tc.scan_live_telegraph_catalysts(
        l2_map,
        quotes,
        since_ts=500.0,
        now_ts=1200.0,
        now_hm="12:50",
        red_ratio=0.30,
        raw_items=raw_items,
    )
    assert len(lines) == 1
    out = lines[0]
    assert "300999" not in out  # +4.5% > +3.8% cap dropped after 11:00
    assert "002261" in out
    assert "🔥3日L2" in out
    assert "⚠️弱市禁追" in out
    assert out.index("002261") < out.index("688316")

    etf_quotes = {"588000.SH": {"amount": 190.0e8}, "159915.SZ": {"amount": 165.0e8}}
    prev_etf = {"588000.SH": {"amt_yi": 170.0}, "159915.SZ": {"amt_yi": 150.0}}
    nt_dt = datetime(2026, 11, 17, 14, 40, tzinfo=CN_TZ)
    _, floor_alert = tc.summarize_national_team_etfs(
        etf_quotes, prev_etf, has_baseline=True, now_dt=nt_dt, red_count=1311, green_count=4134, d_red=-150
    )
    assert "🛡️ **【国家队宽基托底护盘（托指不救股）】**" in floor_alert
    _, rush_alert = tc.summarize_national_team_etfs(
        etf_quotes, prev_etf, has_baseline=True, now_dt=nt_dt, red_count=3467, green_count=1900, d_red=420
    )
    assert "🚀 **【国家队20大宽基ETF 5m放量异动】**" in rush_alert


def test_capex_fcf_headwind_warning_and_freeze_sector_resonance_tag() -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    l2_map = {
        "688008": {
            "name": "澜起科技",
            "sector": "半导体",
            "rps": 35.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "streak_days": 3,
            "funnel_tag": "🔥3日L2+L3连入",
        }
    }
    quotes = {"688008.SH": {"last_price": 101.0, "prev_close": 100.0, "amount": 2e8, "vwap": 100.5}}
    raw_items = [
        {
            "id": "sina_fcf",
            "ts": 1000.0,
            "hm": "08:40",
            "text": "科技巨头三季报AI算力资本开支暴增500%导致自由现金流转负",
        },
        {"id": "sina_normal", "ts": 990.0, "hm": "08:38", "text": "工信部推进国产算力芯片应用生态落地"},
    ]
    lines = tc.scan_live_telegraph_catalysts(
        l2_map, quotes, since_ts=500.0, now_ts=1200.0, now_hm="08:45", red_ratio=0.50, raw_items=raw_items
    )
    assert len(lines) == 2
    assert "宏观FCF逆风校验" in lines[0]
    assert "宏观FCF逆风校验" not in lines[1]

    buckets: dict[str, list[str]] = {"l2_events": []}
    records: list[dict[str, object]] = []
    radar._prepend_sector_resonances(
        {"半导体": ["688008.SH 澜起科技", "688525.SH 佰维存储", "301308.SZ 江波龙"]},
        {},
        l2_map,
        {},
        1000.0,
        buckets,
        records,
        red_count=650,
        total_active=5400,
    )
    assert len(buckets["l2_events"]) == 1
    assert "高相关性冰点(红650家)：防半小时回吐，仅观察不追高" in buckets["l2_events"][0]

    buckets_warm: dict[str, list[str]] = {"l2_events": []}
    radar._prepend_sector_resonances(
        {"半导体": ["688008.SH 澜起科技", "688525.SH 佰维存储", "301308.SZ 江波龙"]},
        {},
        l2_map,
        {},
        1000.0,
        buckets_warm,
        [],
        red_count=2600,
        total_active=5400,
    )
    assert "高相关性冰点" not in buckets_warm["l2_events"][0]


def test_three_layer_core_regime_gate_etf_dedup_lps_and_freeze_fallback(tmp_path, monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    today_d = cn_today()
    today = today_d.isoformat()
    ts_1040 = datetime(today_d.year, today_d.month, today_d.day, 10, 40, tzinfo=CN_TZ).timestamp()

    # 1) Layer 1: DEFENSIVE climate (<38% red with >=100 active stocks) blocks L3 SMS and downgrades to L1 observation
    prev_q: dict[str, dict[str, float]] = {
        "000001.SH": {"pct": -1.2, "amt_yi": 3000.0},
        "399001.SZ": {"pct": -1.5, "amt_yi": 4000.0},
    }
    curr_q: dict[str, dict[str, object]] = {
        "000001.SH": {
            "symbol": "000001.SH",
            "last_price": 3450.0,
            "prev_close": 3500.0,
            "volume": 1e9,
            "amount": 3000e8,
        },
        "399001.SZ": {
            "symbol": "399001.SZ",
            "last_price": 10800.0,
            "prev_close": 11000.0,
            "volume": 1e9,
            "amount": 4000e8,
        },
    }
    for i in range(120):
        sym = f"{600100 + i:06d}.SH"
        is_red = i < 25  # 25/121 ~= 20.7% < 38% -> DEFENSIVE
        prev_q[sym] = {"pct": 0.2 if is_red else -1.5, "amt_yi": 0.5}
        curr_q[sym] = {
            "symbol": sym,
            "last_price": 10.05 if is_red else 9.80,
            "prev_close": 10.0,
            "volume": 5e6,
            "amount": 0.52e8,
            "ext": {"name": f"样本{i}"},
        }
    prev_q["688001.SH"] = {"pct": 1.0, "amt_yi": 0.8}
    curr_q["688001.SH"] = {
        "symbol": "688001.SH",
        "last_price": 10.35,
        "prev_close": 10.0,
        "volume": 1.2e7,
        "amount": 1.236e8,
        "ext": {"name": "逆势孤立芯"},
    }
    l2_map = {
        "688001": {
            "name": "逆势孤立芯",
            "sector": "半导体",
            "rps": 30.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "streak_days": 3,
        }
    }
    holdings = radar.parse_holdings_arg(["600519:100.0:95.0:DEF"])
    assert holdings["600519.SH"]["role"] == "DEF"
    curr_q["600519.SH"] = {
        "symbol": "600519.SH",
        "last_price": 104.5,
        "prev_close": 100.0,
        "volume": 1e6,
        "amount": 1.04e8,
        "ext": {"name": "防守测试"},
    }
    prev_state = {"date": today, "ts": ts_1040 - 300.0, "red": 25, "cooldowns": {}, "quotes": prev_q}
    level_def, _, body_def, _, _ = radar.analyze_snapshot_diff(
        curr_q, prev_state, l2_map, holdings, now_ts=ts_1040, turnover_dir=tmp_path / "to_def"
    )
    assert level_def == "L1"  # L3 SMS blocked in bear freeze!
    assert "防守躲熊态" in body_def
    assert "❄️[防守气候禁追·转观察]" in body_def
    assert "行情源疑似冻盘" in body_def  # 3000+4000 == 7000 unchanged across 300s active session!
    assert "[🛡️防守]" in body_def
    assert "建议止损上移保本线100.00" in body_def

    # 2) Layer 2: Deduplicated Theme ETF leadership merges multiple robotics ETFs & flags low-bias vs overextended
    etf_q = {
        "159530.SZ": {
            "last_price": 1.020,
            "prev_close": 1.000,
            "amount": 5.0e8,
            "vwap": 1.010,
            "ext": {"name": "机器人ETF易方达"},
        },
        "562500.SH": {
            "last_price": 0.812,
            "prev_close": 0.800,
            "amount": 2.0e8,
            "vwap": 0.806,
            "ext": {"name": "机器人ETF华夏"},
        },
        "515880.SH": {
            "last_price": 1.060,
            "prev_close": 1.000,
            "amount": 8.0e8,
            "vwap": 1.020,
            "ext": {"name": "通信ETF"},
        },
        "510300.SH": {
            "last_price": 4.020,
            "prev_close": 4.000,
            "amount": 30.0e8,
            "vwap": 4.010,
            "ext": {"name": "沪深300ETF"},
        },
    }
    etf_md = tc.summarize_theme_etf_leadership(etf_q, red_ratio=0.60, now_hm="10:15")
    assert etf_md.count("物理AI/机器人") == 1  # Deduplicated!
    assert "159530.SZ" in etf_md and "🎯黄金低偏离起涨窗" in etf_md
    assert "515880.SH" in etf_md and "⛔偏离过高禁追" in etf_md
    nt_sum, _ = tc.summarize_national_team_etfs({"510300.SH": {"amount": 30e8}, "159915.SZ": {"amount": 90e8}})
    assert "护盘蓝筹 30.0亿 / 成长弹性 90.0亿" in nt_sum
    phys_theme, _ = tc.match_theme_and_candidates("宇树启动物理AI与世界模型自进化计划", l2_map)
    assert "物理AI" in phys_theme

    # 3) Layer 3: Closing mainline anchors persist on Day 1 and surface LPS pullback-to-VWAP on Day 2
    anchors_file = tmp_path / "anchors.json"
    day1_quotes = {
        **etf_q,
        "601689.SH": {
            "symbol": "601689.SH",
            "last_price": 52.5,
            "prev_close": 50.0,
            "volume": 1e7,
            "amount": 5.2e8,
            "vwap": 51.8,
            "ext": {"name": "拓普集团"},
        },
    }
    l2_day1 = {"601689": {"name": "拓普集团", "sector": "汽车配件", "l2_eligible": True, "streak_days": 2}}
    radar.save_closing_mainline_anchors("2026-11-20", day1_quotes, l2_day1, anchors_path=anchors_file)
    day2_quotes = {
        "159530.SZ": {
            "last_price": 1.015,
            "prev_close": 1.020,
            "open": 1.012,
            "volume": 2e8,
            "amount": 2.024e8,
            "vwap": 1.012,
        },
        "601689.SH": {
            "last_price": 52.8,
            "prev_close": 52.5,
            "open": 52.2,
            "volume": 8e6,
            "amount": 4.2e8,
            "vwap": 52.5,
        },
    }
    lps_md = radar.scan_next_day_lps_pullbacks(
        "2026-11-23", "10:05", day2_quotes, {"601689.SH": {"pct": 0.1}}, True, 0.52, anchors_path=anchors_file
    )
    assert "次日主线分歧回踩承接窗" in lps_md
    assert "159530.SZ" in lps_md and "601689.SH" in lps_md

    # 4) Sina HQ fallback parses live row cleanly when feed is frozen
    fake_sina = 'var hq_str_sz000543="皖能电力,7.65,7.68,7.40,7.66,7.39,7.40,7.41,15000000,112500000,0";\n'
    monkeypatch.setattr(
        radar.urllib.request,
        "urlopen",
        lambda req, timeout=4.0: SimpleNamespace(read=lambda: fake_sina.encode("gbk")),
    )
    sina_map = radar._fetch_sina_holding_quotes(["000543.SZ"])
    assert sina_map["000543.SZ"]["last_price"] == 7.40


def test_late_day_decision_card_and_cli_flag(tmp_path, monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    qualifying_etfs = {
        "562500.SH": {
            "symbol": "562500.SH",
            "last_price": 1.000,
            "prev_close": 0.960,
            "high": 1.005,
            "low": 0.965,
            "volume": 3.0e8,
            "amount": 2.97e8,  # vwap=0.990 -> vwap_dev=+1.01%, upper_wick=0.50%, pct=+4.17%, amt=2.97亿
            "ext": {"name": "机器人ETF华夏"},
        },
        "159915.SZ": {
            "symbol": "159915.SZ",
            "last_price": 2.000,
            "prev_close": 1.960,
            "high": 2.010,
            "low": 1.960,
            "volume": 5.0e8,
            "amount": 9.90e8,  # vwap=1.980 -> vwap_dev=+1.01%, upper_wick=0.50%, pct=+2.04%
            "ext": {"name": "创业板ETF易方达"},
        },
    }

    # 1) DEFENSIVE / CAUTIOUS climate (<55% red) refuses new positions
    cautious_card = radar.build_late_day_decision_card(qualifying_etfs, red_count=45, total_active=100)
    assert "今日不开新仓 · 握纯现金/511360" in cautious_card

    # 2) ATTACK climate, but both SH and CY indices show >1.2% upper wick -> skip
    wick_indices = {
        **qualifying_etfs,
        "000001.SH": {"last_price": 3500.0, "prev_close": 3480.0, "high": 3560.0, "low": 3475.0, "amount": 5000e8},
        "399006.SZ": {"last_price": 2200.0, "prev_close": 2180.0, "high": 2250.0, "low": 2170.0, "amount": 3000e8},
    }
    wick_card = radar.build_late_day_decision_card(wick_indices, red_count=75, total_active=100)
    assert "今日不开新仓" in wick_card
    assert "防尾盘冲高回落" in wick_card

    # 3) ATTACK climate, but all ETFs overextended (vwap_dev > 2.2% or upper_wick_pct > 1.2%) -> skip
    overext_etfs = {
        "562500.SH": {
            "symbol": "562500.SH",
            "last_price": 1.050,
            "prev_close": 1.000,
            "high": 1.052,
            "low": 1.000,
            "volume": 3.0e8,
            "amount": 3.00e8,  # vwap=1.000 -> vwap_dev=+5.00%
            "ext": {"name": "机器人ETF华夏"},
        },
        "159915.SZ": {
            "symbol": "159915.SZ",
            "last_price": 2.000,
            "prev_close": 1.960,
            "high": 2.050,  # upper_wick = 2.44% > 1.2%
            "low": 1.960,
            "volume": 5.0e8,
            "amount": 9.95e8,
            "ext": {"name": "创业板ETF易方达"},
        },
    }
    overext_card = radar.build_late_day_decision_card(overext_etfs, red_count=80, total_active=100)
    assert "今日不开新仓 · 留现金等分歧" in overext_card

    # 4) ATTACK climate with healthy qualifying ETFs -> outputs -3% stop and +4% breakeven
    ok_card = radar.build_late_day_decision_card(qualifying_etfs, red_count=75, total_active=100)
    assert "顺势可介入主线 ETF" in ok_card
    assert "562500.SH" in ok_card and "159915.SZ" in ok_card
    assert "0.970" in ok_card and "1.040" in ok_card  # 1.000 * 0.97, 1.000 * 1.04
    assert "1.940" in ok_card and "2.080" in ok_card  # 2.000 * 0.97, 2.000 * 1.04

    # 5) End-to-end CLI --late-decision flag via main()
    state_file = tmp_path / "state_late.json"
    monkeypatch.setattr(radar, "STATE_PATH", state_file)
    monkeypatch.setattr(radar, "ALERT_LOG_PATH", tmp_path / "alerts_late.jsonl")
    monkeypatch.setattr(radar, "TURNOVER_DIR", tmp_path / "turnover_late")
    monkeypatch.setattr(radar, "MAINLINE_ANCHORS_PATH", tmp_path / "anchors_late.json")
    monkeypatch.setattr(radar, "load_wyckoff_l2_map", lambda *a, **kw: {"600100": {"l2_eligible": True}})
    monkeypatch.setattr(radar, "resolve_lark_open_id", lambda *a: "ou_mock")
    monkeypatch.setattr(tc, "fetch_sina_telegraph", lambda pages=1: [])
    monkeypatch.setattr(tc, "fetch_wscn_telegraph", lambda: [])

    quotes_cli = dict(qualifying_etfs)
    for i in range(100):
        sym = f"{600100 + i:06d}.SH"
        is_up = i < 70
        quotes_cli[sym] = {
            "symbol": sym,
            "last_price": 10.2 if is_up else 9.8,
            "prev_close": 10.0,
            "volume": 1e6,
            "amount": 1e7,
            "ext": {"name": f"股{i}"},
        }
    quotes_cli["000001.SH"] = {
        "last_price": 3510.0,
        "prev_close": 3500.0,
        "high": 3515.0,
        "low": 3495.0,
        "amount": 5000e8,
    }
    quotes_cli["399001.SZ"] = {
        "last_price": 11050.0,
        "prev_close": 11000.0,
        "high": 11060.0,
        "low": 10980.0,
        "amount": 6000e8,
    }

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get_quotes(self, universes=None, symbols=None):
            return quotes_cli

    import integrations.tickflow_client as tf_mod

    monkeypatch.setattr(tf_mod, "TickFlowClient", FakeClient)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(radar, "send_feishu_tiered_alert", lambda t, c, **kw: sent.append((t, c)) or "om_late")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), "--once", "--force-send", "--late-decision", "--ignore-hours"])
    radar.main()
    assert len(sent) == 1
    assert sent[0][0].startswith("🎯【14:40 尾盘明牌决策卡】")
    assert "顺势可介入主线 ETF" in sent[0][1]
    assert "0.970" in sent[0][1] and "1.040" in sent[0][1]


def test_two_regime_t1_chip_structure_gate(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    today_d = cn_today()
    today = today_d.isoformat()
    t_dir = tmp_path / "turnover_chip"

    def _make_quotes(cy_open: float, cy_last: float, etf_last: float, etf_vwap: float, red_n: int = 62):
        q: dict[str, dict] = {
            "000001.SH": {
                "symbol": "000001.SH",
                "open": 3500.0,
                "prev_close": 3500.0,
                "last_price": 3515.0,
                "high": 3520.0,
                "low": 3495.0,
                "amount": 3000e8,
            },
            "399001.SZ": {
                "symbol": "399001.SZ",
                "open": 10500.0,
                "prev_close": 10500.0,
                "last_price": 10560.0,
                "high": 10580.0,
                "low": 10480.0,
                "amount": 3500e8,
            },
            "399006.SZ": {
                "symbol": "399006.SZ",
                "open": cy_open,
                "prev_close": 2212.0 if cy_last < 2260 else 2280.0,
                "last_price": cy_last,
                "high": max(cy_open, cy_last) * 1.003,
                "low": min(cy_open, cy_last) * 0.997,
                "amount": 1500e8,
            },
            "562500.SH": {
                "symbol": "562500.SH",
                "last_price": etf_last,
                "prev_close": 0.764 if etf_last < 0.80 else 0.815,
                "high": etf_last * 1.004,
                "low": etf_last * 0.99,
                "volume": 3.0e8,
                "amount": 3.0e8,
                "vwap": etf_vwap,
                "ext": {"name": "机器人ETF华夏"},
            },
        }
        for i in range(100):
            sym = f"{600100 + i:06d}.SH"
            q[sym] = {
                "symbol": sym,
                "last_price": 10.2 if i < red_n else 9.8,
                "prev_close": 10.0,
                "volume": 1e6,
                "amount": 1e7,
                "ext": {"name": f"样本{i}"},
            }
        return q

    # 1) Washout First Day (2026-11-23 archetype): multi-day drop -> low T-1 close -> opens morning early window
    washout_bars = {
        "399006.SZ": [
            {"day": "d1", "open": 2300, "high": 2343, "low": 2264, "close": 2292, "volume": 1.5e10},
            {"day": "d2", "open": 2290, "high": 2315, "low": 2272, "close": 2293, "volume": 1.4e10},
            {"day": "d3", "open": 2244, "high": 2263, "low": 2212, "close": 2212, "volume": 1.6e10},
        ],
        "510300.SH": [
            {"day": "d1", "open": 4.04, "high": 4.06, "low": 4.01, "close": 4.04, "volume": 2.0e9},
            {"day": "d2", "open": 4.05, "high": 4.07, "low": 4.01, "close": 4.06, "volume": 2.5e9},
            {"day": "d3", "open": 4.01, "high": 4.03, "low": 3.97, "close": 4.01, "volume": 4.2e9},
        ],
        "562500.SH": [
            {"day": "d1", "open": 0.820, "high": 0.830, "low": 0.802, "close": 0.810, "volume": 6.8e8},
            {"day": "d2", "open": 0.798, "high": 0.807, "low": 0.790, "close": 0.796, "volume": 4.7e8},
            {"day": "d3", "open": 0.781, "high": 0.788, "low": 0.764, "close": 0.764, "volume": 6.3e8},
        ],
    }
    q_wash = _make_quotes(cy_open=2200.0, cy_last=2235.0, etf_last=0.775, etf_vwap=0.770, red_n=62)
    reg_w, banner_w = tc.evaluate_t1_chip_regime(q_wash, washout_bars, red_ratio=0.62)
    assert reg_w == "WASHOUT_FIRST_DAY"
    assert "恐慌出清首日" in banner_w
    ts_0955 = datetime(today_d.year, today_d.month, today_d.day, 9, 55, tzinfo=CN_TZ).timestamp()
    _, _, body_w, _, _ = radar.analyze_snapshot_diff(
        q_wash, {}, {}, {}, now_ts=ts_0955, turnover_dir=t_dir, daily_bars=washout_bars
    )
    assert "恐慌出清首日·开放早盘先手窗" in body_w
    assert "早盘冰点出清先手卡" in body_w
    assert "562500.SH" in body_w and "0.752" in body_w and "0.806" in body_w

    # 2) Chip Dump Lock Day (2026-11-24 archetype): T-1 big bounce w/ shrunk 510300 + gap-up into 0.830 peak
    dump_bars = {
        "399006.SZ": [
            {"day": "d1", "open": 2290, "high": 2315, "low": 2272, "close": 2293, "volume": 1.4e10},
            {"day": "d2", "open": 2244, "high": 2263, "low": 2212, "close": 2212, "volume": 1.6e10},
            {"day": "d3", "open": 2195, "high": 2297, "low": 2188, "close": 2280, "volume": 1.88e10},
        ],
        "510300.SH": [
            {"day": "d1", "open": 4.05, "high": 4.07, "low": 4.01, "close": 4.06, "volume": 2.8e9},
            {"day": "d2", "open": 4.01, "high": 4.03, "low": 3.97, "close": 4.01, "volume": 4.5e9},
            {"day": "d3", "open": 3.99, "high": 4.04, "low": 3.97, "close": 4.02, "volume": 1.39e9},
        ],
        "562500.SH": [
            {"day": "d1", "open": 0.820, "high": 0.830, "low": 0.802, "close": 0.810, "volume": 6.8e8},
            {"day": "d2", "open": 0.781, "high": 0.788, "low": 0.764, "close": 0.764, "volume": 6.3e8},
            {"day": "d3", "open": 0.755, "high": 0.820, "low": 0.753, "close": 0.815, "volume": 1.11e9},
        ],
    }
    q_dump = _make_quotes(cy_open=2368.0, cy_last=2330.0, etf_last=0.842, etf_vwap=0.833, red_n=65)
    q_dump["688001.SH"] = {
        "symbol": "688001.SH",
        "last_price": 10.35,
        "prev_close": 10.0,
        "open": 10.1,
        "high": 10.38,
        "low": 10.05,
        "volume": 1.2e7,
        "amount": 1.22e8,
        "vwap": 10.30,
        "ext": {"name": "筹码锁测试"},
    }
    reg_d, banner_d = tc.evaluate_t1_chip_regime(q_dump, dump_bars, red_ratio=0.65)
    assert reg_d == "CHIP_DUMP_LOCK"
    assert "T+1 筹码兑现禁买令" in banner_d
    ts_1100 = datetime(today_d.year, today_d.month, today_d.day, 11, 0, tzinfo=CN_TZ).timestamp()
    prev_q_snap = {s: {"pct": 1.0, "amt_yi": 0.70} for s in q_dump}
    l2_map = {
        "688001": {
            "name": "筹码锁测试",
            "sector": "半导体",
            "rps": 20.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "is_candidate": True,
            "channels": ["地量蓄势"],
            "is_springboard": False,
        }
    }
    lvl_d, _, body_d, _, _ = radar.analyze_snapshot_diff(
        q_dump,
        {"date": today, "ts": ts_1100 - 300.0, "red": 65, "cooldowns": {}, "quotes": prev_q_snap},
        l2_map,
        {},
        now_ts=ts_1100,
        turnover_dir=t_dir,
        daily_bars=dump_bars,
    )
    assert lvl_d == "L1"
    assert "💣T+1获利盘+" in body_d and "撞前高0.830·禁早盘追买(等14:40)" in body_d
    assert "🎯黄金低偏离起涨窗" not in body_d
    assert "💣[T+1筹码兑现期禁追·转观察]" in body_d

    # 3) 14:40 tightening upper-wick limit (<=0.8%) when kissing prior trapped peak 0.830
    q_late_wick = {
        "562500.SH": {
            "symbol": "562500.SH",
            "last_price": 0.835,
            "prev_close": 0.815,
            "high": 0.844,  # upper_wick = 1.07% > 0.8%
            "low": 0.812,
            "volume": 3.5e8,
            "amount": 2.8875e8,  # vwap = 0.825 -> vwap_dev = +1.21%
            "ext": {"name": "机器人ETF华夏"},
        }
    }
    rejected = radar.build_late_day_decision_card(q_late_wick, red_count=75, total_active=100, daily_bars=dump_bars)
    assert "今日不开新仓" in rejected
    q_late_wick["562500.SH"]["high"] = 0.838  # upper_wick = 0.36% <= 0.8%
    accepted = radar.build_late_day_decision_card(q_late_wick, red_count=75, total_active=100, daily_bars=dump_bars)
    assert "顺势可介入主线 ETF" in accepted and "562500.SH" in accepted

    # 4) Exclude cross-border QDII ETFs (even on huge gains) so A-share theme ranking is never polluted
    qdii_quotes = {
        **q_late_wick,
        "159509.SZ": {
            "symbol": "159509.SZ",
            "last_price": 1.080,
            "prev_close": 1.000,
            "high": 1.081,
            "low": 1.050,
            "volume": 5.0e8,
            "amount": 5.3e8,
            "ext": {"name": "纳指科技ETF"},
        },
        "513800.SH": {
            "symbol": "513800.SH",
            "last_price": 1.060,
            "prev_close": 1.000,
            "high": 1.061,
            "low": 1.040,
            "volume": 3.0e8,
            "amount": 3.15e8,
            "ext": {"name": "日本东证ETF"},
        },
    }
    ranked_syms = {r["symbol"] for r in tc.rank_deduplicated_theme_etfs(qdii_quotes, min_amt_yi=1.5, top_n=6)}
    assert "562500.SH" in ranked_syms
    assert "159509.SZ" not in ranked_syms and "513800.SH" not in ranked_syms
    late_no_qdii = radar.build_late_day_decision_card(qdii_quotes, red_count=75, total_active=100, daily_bars=dump_bars)
    assert "159509.SZ" not in late_no_qdii and "513800.SH" not in late_no_qdii and "562500.SH" in late_no_qdii


def test_t1_locked_holding_mutes_phone_on_buy_day_and_unlocks_next_day(tmp_path) -> None:
    from datetime import timedelta

    radar = _load_radar_module()
    today_d = cn_today()
    next_d = today_d + timedelta(days=1)
    buy_ymd = today_d.strftime("%Y%m%d")
    holdings = radar.parse_holdings_arg([f"562500.SH:0.842:0.818:ATK:{buy_ymd}"])
    assert holdings["562500.SH"]["role"] == "ATK"
    assert holdings["562500.SH"]["buy_date"] == today_d.isoformat()

    quotes = {
        "562500.SH": {
            "symbol": "562500.SH",
            "last_price": 0.815,
            "prev_close": 0.815,
            "volume": 2e8,
            "amount": 1.66e8,
            "ext": {"name": "机器人ETF华夏"},
        }
    }
    ts_same_day = datetime(today_d.year, today_d.month, today_d.day, 14, 10, tzinfo=CN_TZ).timestamp()
    lvl_same, _, body_same, _, cds_same = radar.analyze_snapshot_diff(
        quotes, {}, {}, holdings, now_ts=ts_same_day, turnover_dir=tmp_path
    )
    assert lvl_same == "L1"
    assert "🔒[今日T+1锁仓" in body_same
    assert "L4:holding_stop:562500.SH" not in cds_same

    ts_next_day = datetime(next_d.year, next_d.month, next_d.day, 9, 35, tzinfo=CN_TZ).timestamp()
    assert not radar._is_t1_locked(holdings["562500.SH"], ts_next_day)
    lvl_next, _, body_next, _, _ = radar.analyze_snapshot_diff(
        quotes, {}, {}, holdings, now_ts=ts_next_day, turnover_dir=tmp_path
    )
    assert lvl_next == "L4"
    assert "L4 破位止损" in body_next


def test_etf_heavyweight_divergence_and_anti_laggard_substitute_gate() -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()

    # 1) 562500 up +1.5% on low VWAP dev, yet its 5 top industrial-control heavyweights are dragging red
    divergent_quotes = {
        "562500.SH": {
            "symbol": "562500.SH",
            "last_price": 0.827,
            "prev_close": 0.815,
            "high": 0.829,
            "low": 0.810,
            "volume": 3.0e8,
            "amount": 2.469e8,  # vwap = 0.823 -> dev=+0.49%
            "ext": {"name": "机器人ETF华夏"},
        },
        "300124.SZ": {"last_price": 58.8, "prev_close": 60.0},  # -2.0%
        "002230.SZ": {"last_price": 50.05, "prev_close": 50.0},  # +0.1%
        "688169.SH": {"last_price": 198.6, "prev_close": 200.0},  # -0.7%
        "002236.SZ": {"last_price": 19.84, "prev_close": 20.0},  # -0.8%
        "688777.SH": {"last_price": 45.09, "prev_close": 45.0},  # +0.2%
    }
    warn = tc.check_etf_heavyweight_divergence("562500.SH", "物理AI/机器人", divergent_quotes)
    assert "⚠️重仓股背离" in warn
    etf_md = tc.summarize_theme_etf_leadership(divergent_quotes, red_ratio=0.60, now_hm="14:40")
    assert "禁买错配ETF" in etf_md
    late_div = radar.build_late_day_decision_card(divergent_quotes, red_count=65, total_active=100)
    assert "562500.SH" not in late_div and "宁缺毋滥禁买后排平替" in late_div

    # 2) Pure anchor ETF 159530.SZ takes precedence over broad-heavy 562500.SH
    pair_quotes = {
        "562500.SH": {
            "symbol": "562500.SH",
            "last_price": 0.822,
            "prev_close": 0.815,
            "amount": 2.2e8,
            "vwap": 0.820,
            "ext": {"name": "机器人ETF华夏"},
        },
        "159530.SZ": {
            "symbol": "159530.SZ",
            "last_price": 1.230,
            "prev_close": 1.215,
            "amount": 2.0e8,
            "vwap": 1.224,
            "ext": {"name": "机器人ETF易方达"},
        },
    }
    ranked = tc.rank_deduplicated_theme_etfs(pair_quotes, min_amt_yi=1.0, top_n=4)
    assert len(ranked) == 1 and ranked[0]["symbol"] == "159530.SZ"

    # 3) Anti-laggard-substitute: when #1 leader overextends >=4.5%, weak follower (<50% of leader pct) is blocked
    sub_quotes = {
        "515880.SH": {
            "symbol": "515880.SH",
            "last_price": 1.068,
            "prev_close": 1.000,
            "high": 1.070,
            "low": 1.020,
            "volume": 5.0e8,
            "amount": 5.16e8,  # vwap = 1.032 -> dev=+3.49%, pct=+6.8%
            "ext": {"name": "通信ETF"},
        },
        "512660.SH": {
            "symbol": "512660.SH",
            "last_price": 1.018,
            "prev_close": 1.000,
            "high": 1.021,
            "low": 1.002,
            "volume": 3.0e8,
            "amount": 3.039e8,  # vwap = 1.013 -> dev=+0.49%, pct=+1.8% (< 6.8*0.5)
            "ext": {"name": "军工ETF"},
        },
    }
    no_sub = radar.build_late_day_decision_card(sub_quotes, red_count=60, total_active=100)
    assert "512660.SH" not in no_sub and "宁缺毋滥禁买后排平替" in no_sub


def test_main_rise_pre_breakout_and_intraday_rs_leaders(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()

    # 1) Multi-day trace merge preserves MainRise (RPS=72) & pre_breakout (RPS=66) without t1_isolated_spike wipeout
    t_m2 = {
        "symbols": {
            "301095": {
                "name": "广立微",
                "sector": "半导体",
                "rps_fast": 68.0,
                "l2_eligible": False,
                "l3_eligible": False,
                "stage": "观察",
            }
        }
    }
    t_m1 = {
        "symbols": {
            "301095": {
                "name": "广立微",
                "sector": "半导体",
                "rps_fast": 72.0,
                "l2_eligible": True,
                "l3_eligible": True,
                "l2_channel": "主升通道",
                "stage": "候选池已捕获",
            },
            "688001": {
                "name": "前夜芯",
                "sector": "半导体",
                "rps_fast": 66.0,
                "l2_eligible": True,
                "l3_eligible": False,
                "shadow_lane": "pre_breakout",
                "stage": "买点未确认",
            },
        }
    }
    for d, payload in (("20261120", t_m2), ("20261123", t_m1)):
        with gzip.open(tmp_path / f"review_trace_{d}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(payload, f)
    merged = radar.load_wyckoff_l2_map(trace_dirs=[str(tmp_path)], max_age_days=None, auto_fetch=False)
    assert merged["301095"]["is_candidate"] and not merged["301095"].get("t1_isolated_spike")
    assert merged["688001"]["shadow_lane"] == "pre_breakout" and not merged["688001"].get("t1_isolated_spike")

    # 2) Intraday RS selector picks MainRise & >=20亿 flat-open giants, rejects gap traps & bare RPS>88 hype
    l2_rs = {
        **merged,
        "688256": {"name": "寒武纪", "sector": "半导体", "rps": 67.0, "l2_eligible": False, "l3_eligible": False},
        "688008": {
            "name": "澜起科技",
            "sector": "半导体",
            "rps": 70.0,
            "l2_eligible": True,
            "l3_eligible": True,
            "channels": ["主升通道"],
        },
        "301413": {"name": "安培龙", "sector": "电器仪表", "rps": 99.1, "l2_eligible": False, "l3_eligible": False},
    }
    rs_quotes = {
        "301095.SZ": {
            "symbol": "301095.SZ",
            "open": 10.13,
            "prev_close": 10.00,
            "last_price": 10.67,  # +6.70% (above old 5.0% cap!), open=+1.3%, amt=16.1亿
            "high": 10.69,
            "low": 10.08,
            "volume": 1.5e8,
            "amount": 1.584e9,  # vwap=10.56 -> dev=+1.04%, wick=0.19%
            "ext": {"name": "广立微"},
        },
        "688256.SH": {
            "symbol": "688256.SH",
            "open": 102.75,
            "prev_close": 100.0,
            "last_price": 106.03,  # +6.03%, open=+2.75%, amt=143亿 >= 20亿 giant!
            "high": 106.20,
            "low": 102.00,
            "volume": 1.36e8,
            "amount": 1.432e10,  # vwap=105.29 -> dev=+0.70%
            "ext": {"name": "寒武纪"},
        },
        "688008.SH": {
            "symbol": "688008.SH",
            "open": 108.17,  # open +8.17% > 3.8% gap trap!
            "prev_close": 100.0,
            "last_price": 109.0,
            "high": 109.2,
            "low": 107.0,
            "volume": 3e7,
            "amount": 3.24e9,
            "ext": {"name": "澜起科技"},
        },
        "301413.SZ": {
            "symbol": "301413.SZ",
            "open": 20.4,  # open +2.0%, amt=16亿 < 20亿 and bare RPS=99.1 > 88 -> rejected!
            "prev_close": 20.0,
            "last_price": 21.1,
            "high": 21.15,
            "low": 20.3,
            "volume": 7.7e7,
            "amount": 1.6e9,
            "ext": {"name": "安培龙"},
        },
        "588000.SH": {
            "symbol": "588000.SH",
            "last_price": 1.33,
            "prev_close": 1.30,
            "high": 1.335,
            "low": 1.295,
            "volume": 5e9,
            "amount": 6.58e9,
            "ext": {"name": "科创50ETF华夏"},
        },
    }
    rs_picked = tc.rank_intraday_rs_leaders(rs_quotes, l2_rs, top_n=4)
    rs_syms = [r["symbol"] for r in rs_picked]
    assert "301095.SZ" in rs_syms and "688256.SH" in rs_syms
    assert "688008.SH" not in rs_syms and "301413.SZ" not in rs_syms

    # 3) 14:40 card shows RS core stocks on ATTACK, keeps DEFENSIVE (<38%) cash-only
    att_card = radar.build_late_day_decision_card(rs_quotes, red_count=65, total_active=100, l2_map=l2_rs)
    assert "尾盘抗跌主升/百亿中军备选" in att_card and "301095.SZ" in att_card and "688256.SH" in att_card
    def_card = radar.build_late_day_decision_card(rs_quotes, red_count=34, total_active=100, l2_map=l2_rs)
    assert "今日不开新仓 · 握纯现金/511360" in def_card and "301095.SZ" not in def_card


def test_holding_morning_gap_trim_and_spike_rollover_l2_alerts(tmp_path) -> None:
    radar = _load_radar_module()
    today_d = cn_today()
    today_s = today_d.isoformat()
    buy_ymd = today_d.strftime("%Y%m%d")
    ts_0935 = datetime(today_d.year, today_d.month, today_d.day, 9, 35, tzinfo=CN_TZ).timestamp()
    ts_1005 = datetime(today_d.year, today_d.month, today_d.day, 10, 5, tzinfo=CN_TZ).timestamp()

    # 1) Unlocked ATK holding at 09:35 gap-up (+4.18% open -> +5.30%, vwap_dev +0.53% < 3.0%) fires L2 morning trim
    gap_quotes = {
        "603011.SH": {
            "symbol": "603011.SH",
            "prev_close": 14.34,
            "open": 14.94,
            "last_price": 15.10,
            "high": 15.14,
            "low": 14.90,
            "volume": 1.0e7,
            "amount": 1.502e8,
            "ext": {"name": "合锻智能"},
        }
    }
    atk_holdings = radar.parse_holdings_arg(["603011.SH:18.252:13.00:ATK"])
    lvl_trim, title_trim, body_trim, _, cds_trim = radar.analyze_snapshot_diff(
        gap_quotes, {}, {}, atk_holdings, now_ts=ts_0935, turnover_dir=tmp_path / "to1"
    )
    assert lvl_trim == "L2"
    assert "【L2 加急焦点】" in title_trim
    assert "【老仓高开兑现】603011.SH 合锻智能" in body_trim
    assert "A股存量博弈下早盘借利好高开冲高是兑现窗口而非追涨号角" in body_trim
    assert cds_trim.get("L2:holding_morning_trim:603011.SH") == ts_0935

    # 2) Same-day T+1 locked holding up +5.30% at 09:35 stays L1 muted
    locked_holdings = radar.parse_holdings_arg([f"603011.SH:14.34:13.00:ATK:{buy_ymd}"])
    lvl_locked, _, body_locked, _, cds_locked = radar.analyze_snapshot_diff(
        gap_quotes, {}, {}, locked_holdings, now_ts=ts_0935, turnover_dir=tmp_path / "to2"
    )
    assert lvl_locked == "L1"
    assert "🔒[今日T+1锁仓" in body_locked
    assert "L2:holding_morning_trim:603011.SH" not in cds_locked

    # 3) DEF role holding up +3.80% at 09:35 (vwap_dev +0.5%) does NOT fire 老仓高开兑现
    def_quotes = {
        "601088.SH": {
            "symbol": "601088.SH",
            "prev_close": 40.00,
            "open": 41.40,
            "last_price": 41.52,
            "high": 41.60,
            "low": 41.20,
            "volume": 1.0e7,
            "amount": 4.131e8,
            "ext": {"name": "中国神华"},
        }
    }
    def_holdings = radar.parse_holdings_arg(["601088.SH:39.00:37.50:DEF"])
    lvl_def, _, body_def, _, _ = radar.analyze_snapshot_diff(
        def_quotes, {}, {}, def_holdings, now_ts=ts_0935, turnover_dir=tmp_path / "to3"
    )
    assert lvl_def == "L1"
    assert "老仓高开兑现" not in body_def

    # 4) WASHOUT_FIRST_DAY morning suppresses premature 老仓高开兑现
    washout_bars = {
        "399006.SZ": [
            {"day": "t-3", "open": 2380.0, "high": 2400.0, "low": 2310.0, "close": 2320.0, "volume": 1.0e9},
            {"day": "t-2", "open": 2310.0, "high": 2320.0, "low": 2240.0, "close": 2250.0, "volume": 1.0e9},
            {"day": "t-1", "open": 2245.0, "high": 2255.0, "low": 2200.0, "close": 2210.0, "volume": 1.0e9},
        ]
    }
    washout_quotes = {
        "399006.SZ": {
            "symbol": "399006.SZ",
            "prev_close": 2210.0,
            "open": 2214.4,
            "last_price": 2250.0,
            "high": 2255.0,
            "low": 2208.0,
            "volume": 1.0e9,
            "amount": 2.23e11,
            "ext": {"name": "创业板指"},
        },
        "603011.SH": {
            "symbol": "603011.SH",
            "prev_close": 14.34,
            "open": 14.63,  # +2.02% open would trigger without WASHOUT_FIRST_DAY guard
            "last_price": 14.88,
            "high": 14.90,
            "low": 14.55,
            "volume": 1.0e7,
            "amount": 1.47e8,
            "ext": {"name": "合锻智能"},
        },
    }
    ts_0940 = datetime(today_d.year, today_d.month, today_d.day, 9, 40, tzinfo=CN_TZ).timestamp()
    lvl_wash, _, body_wash, _, cds_wash = radar.analyze_snapshot_diff(
        washout_quotes,
        {},
        {},
        atk_holdings,
        now_ts=ts_0940,
        turnover_dir=tmp_path / "to4",
        daily_bars=washout_bars,
    )
    assert lvl_wash == "L1"
    assert "恐慌出清首日" in body_wash
    assert "老仓高开兑现" not in body_wash
    assert "L2:holding_morning_trim:603011.SH" not in cds_wash

    # 5) 10:05 rollover: high=+6.35% bleeds slowly (-0.9% 5m, no L3) below VWAP -> L2 rollover profit lock
    roll_prev = {
        "date": today_s,
        "ts": ts_1005 - 300.0,
        "red": 0,
        "cooldowns": dict(cds_trim),
        "quotes": {"603011.SH": {"pct": 3.97, "amt_yi": 2.50}},
    }
    roll_quotes = {
        "603011.SH": {
            "symbol": "603011.SH",
            "prev_close": 14.34,
            "open": 14.94,
            "last_price": 14.78,
            "high": 15.25,
            "low": 14.75,
            "volume": 2.0e7,
            "amount": 2.992e8,
            "ext": {"name": "合锻智能"},
        }
    }
    lvl_roll, _, body_roll, _, cds_roll = radar.analyze_snapshot_diff(
        roll_quotes, roll_prev, {}, atk_holdings, now_ts=ts_1005, turnover_dir=tmp_path / "to5"
    )
    assert lvl_roll == "L2"
    assert "【老仓冲高破均线锁利】603011.SH 合锻智能" in body_roll
    assert "谨防单边派发回吐，建议果断锁利兑现" in body_roll
    assert cds_roll.get("L2:holding_rollover:603011.SH") == ts_1005


def test_mainline_formation_l2_alert_and_pharma_core_followers(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    today_d = cn_today()
    ts_0950 = datetime(today_d.year, today_d.month, today_d.day, 9, 50, tzinfo=CN_TZ).timestamp()

    # Build an 8-stock tiny sector (超市连锁) + 16-stock 大医药 (化学制药/中成药/医药商业) with 4 limit-ups & L2 cores
    l2_map: dict[str, dict] = {}
    quotes: dict[str, dict] = {}

    for i in range(8):
        code = f"6037{i:02d}"
        l2_map[code] = {"name": f"超市{i}", "sector": "超市连锁", "rps": 30.0, "l2_eligible": False}
        quotes[f"{code}.SH"] = {
            "symbol": f"{code}.SH",
            "open": 10.1,
            "prev_close": 10.0,
            "last_price": 10.50,
            "high": 10.55,
            "low": 10.0,
            "volume": 5e6,
            "amount": 5.2e7,  # total tiny sector amt ~4.2亿
            "ext": {"name": f"超市{i}"},
        }

    # 4 limit-ups in 化学制药 + 2 limit-up/strong in 中成药 & 医药商业 -> 16 total, >=35亿 turnover, 0 5m spikes!
    pharma_defs = [
        ("300086", "SZ", "康芝药业", "化学制药", 20.0, 2.5e8, 7.2, False, False, "", []),
        ("000756", "SZ", "新华制药", "化学制药", 10.0, 2.8e8, 65.3, False, False, "", []),
        ("600488", "SH", "津药药业", "化学制药", 10.0, 2.2e8, 55.7, False, False, "", []),
        ("002275", "SZ", "桂林三金", "中成药", 10.0, 5.5e8, 92.1, False, False, "", []),
        ("600196", "SH", "复星医药", "化学制药", 3.83, 6.0e8, 78.6, True, False, "", ["地量蓄势"]),
        ("603233", "SH", "大参林", "医药商业", 2.75, 3.2e8, 97.6, True, False, "", ["主升通道"]),
    ]
    for code, ex, name, sec, pct, amt, rps, l2_el, l3_el, lane, chs in pharma_defs:
        sym = f"{code}.{ex}"
        l2_map[code] = {
            "name": name,
            "sector": sec,
            "rps": rps,
            "l2_eligible": l2_el,
            "l3_eligible": l3_el,
            "shadow_lane": lane,
            "channels": chs,
        }
        pre = 10.0
        last = round(pre * (1.0 + pct / 100.0), 3)
        opn = round(pre * 1.002, 3)
        vwap = round(last / 1.007, 3)
        quotes[sym] = {
            "symbol": sym,
            "prev_close": pre,
            "open": opn,
            "last_price": last,
            "high": round(last * 1.004, 3),
            "low": round(pre * 0.998, 3),
            "vwap": vwap,
            "amount": amt,
            "ext": {"name": name},
        }
    for i in range(10):
        code = f"6009{i:02d}"
        sec = "化学制药" if i < 6 else ("中成药" if i < 8 else "医药商业")
        l2_map[code] = {"name": f"药企{i}", "sector": sec, "rps": 45.0, "l2_eligible": False}
        quotes[f"{code}.SH"] = {
            "symbol": f"{code}.SH",
            "prev_close": 10.0,
            "open": 10.02,
            "last_price": 10.22,
            "high": 10.25,
            "low": 9.98,
            "vwap": 10.12,
            "amount": 2.1e8,
            "ext": {"name": f"药企{i}"},
        }
    # Bare non-L2 high-RPS stock must still stay blocked from rank_intraday_rs_leaders
    l2_map["301413"] = {"name": "安培龙", "sector": "电器仪表", "rps": 99.1, "l2_eligible": False}
    quotes["301413.SZ"] = {
        "symbol": "301413.SZ",
        "prev_close": 20.0,
        "open": 20.4,
        "last_price": 21.1,
        "high": 21.15,
        "low": 20.3,
        "vwap": 20.85,
        "amount": 1.6e9,
        "ext": {"name": "安培龙"},
    }

    # 1) Mainline summary ranks pharma sectors ahead of 8-stock micro sector on conviction
    ml_md = tc.summarize_top_sector_mainlines(quotes, l2_map, top_n=5)
    assert "TOP 5" in ml_md
    assert "化学制药" in ml_md
    assert "🔥3涨停" in ml_md
    assert ml_md.index("化学制药") < ml_md.index("超市连锁")

    # 2) At 09:50 with ZERO 5m spike baseline, analyze_snapshot_diff fires L2 mainline_formed for 大医药
    lvl, title, body, _, cds = radar.analyze_snapshot_diff(
        quotes, {}, l2_map, {}, now_ts=ts_0950, turnover_dir=tmp_path / "to_ml"
    )
    assert lvl == "L2"
    assert "【L2 加急焦点】" in title
    assert "【主线成型·中军战法】大医药" in body
    assert "4只涨停" in body
    assert "复星医药" in body and "大参林" in body
    assert "真主线已形成普涨+涨停潮共振" in body
    assert cds.get("L2:mainline_formed:大医药") == ts_0950

    # 3) rank_intraday_rs_leaders promotes 🔥主线中军 (复星医药 & 大参林) while keeping bare RPS>88 blocked
    rs = tc.rank_intraday_rs_leaders(quotes, l2_map, top_n=4)
    rs_map = {r["symbol"]: r for r in rs}
    assert "600196.SH" in rs_map and rs_map["600196.SH"]["tag"] == "🔥主线中军"
    assert "603233.SH" in rs_map and rs_map["603233.SH"]["tag"] == "🔥主线中军"
    assert "301413.SZ" not in rs_map


def test_call_auction_mainline_radar_and_panic_day_flat_open_core_filter(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    today_d = cn_today()
    ts_0926 = datetime(today_d.year, today_d.month, today_d.day, 9, 26, tzinfo=CN_TZ).timestamp()
    ts_0950 = datetime(today_d.year, today_d.month, today_d.day, 9, 50, tzinfo=CN_TZ).timestamp()
    ts_1035 = datetime(today_d.year, today_d.month, today_d.day, 10, 35, tzinfo=CN_TZ).timestamp()

    l2_map: dict[str, dict] = {}
    quotes: dict[str, dict] = {
        "000001.SH": {
            "symbol": "000001.SH",
            "prev_close": 3500.0,
            "open": 3465.0,
            "last_price": 3470.0,
            "amount": 7.0e9,  # 70亿 + 50亿 = 120亿 <= 500亿 pre-open threshold
        },
        "399001.SZ": {
            "symbol": "399001.SZ",
            "prev_close": 10000.0,
            "open": 9850.0,
            "last_price": 9880.0,
            "amount": 5.0e9,
        },
    }

    def _add_stock(
        code: str,
        ex: str,
        name: str,
        sec: str,
        open_pct: float,
        pct: float,
        amt: float,
        *,
        rps: float = 45.0,
        l2_el: bool = False,
        chs: list[str] | None = None,
    ) -> None:
        sym = f"{code}.{ex}"
        l2_map[code] = {"name": name, "sector": sec, "rps": rps, "l2_eligible": l2_el, "channels": chs or []}
        pre = 10.0
        last = round(pre * (1.0 + pct / 100.0), 3)
        opn = round(pre * (1.0 + open_pct / 100.0), 3)
        vwap = round(last / 1.006, 3) if pct >= 0 else round(last * 1.005, 3)
        quotes[sym] = {
            "symbol": sym,
            "prev_close": pre,
            "open": opn,
            "last_price": last,
            "high": round(max(last, opn) * 1.003, 3),
            "low": round(min(last, opn) * 0.996, 3),
            "vwap": vwap,
            "amount": amt,
            "ext": {"name": name},
        }

    # 1) 120 bleeding tech / broad stocks -> 19/159 = 11.9% open red (< 20% panic open)
    for i in range(120):
        _add_stock(f"688{i:03d}", "SH", f"芯片{i}", "半导体", -2.5, -3.2, 2.5e8, rps=60.0)
    # 2) 23 大医药: crowded 生物制药 gap-up + in-sector gap-up trap 600999 + 4 limit-ups + flat-open cores
    _add_stock("300122", "SZ", "高开生物陷", "生物制药", 2.5, 4.2, 6.0e8, rps=70.0, l2_el=True, chs=["主升通道"])
    for i in range(5):
        _add_stock(f"3005{i:02d}", "SZ", f"生物{i}", "生物制药", 2.1, -0.5, 2.0e8)
    _add_stock("600999", "SH", "高开化药陷", "化学制药", 2.2, 4.5, 7.0e8, rps=75.0, l2_el=True, chs=["主升通道"])
    _add_stock("600196", "SH", "复星医药", "化学制药", 0.2, 3.6, 5.5e8, rps=78.0, l2_el=True, chs=["地量蓄势"])
    _add_stock("603233", "SH", "大参林", "医药商业", 0.0, 2.7, 3.8e8, rps=82.0, l2_el=True, chs=["主升通道"])
    for i in range(4):
        _add_stock(f"0027{i:02d}", "SZ", f"药板{i}", "化学制药", 0.6, 10.0, 2.5e8)
    for i in range(4):
        _add_stock(f"6007{i:02d}", "SH", f"化药红{i}", "化学制药", -0.1, 1.5, 2.0e8)
    for i in range(4):
        _add_stock(f"6008{i:02d}", "SH", f"中成{i}", "中成药", -0.2, 1.5, 2.0e8)
    for i in range(2):
        _add_stock(f"6033{i:02d}", "SH", f"药商{i}", "医药商业", -0.1, 1.2, 2.0e8)
    # 3) 16 商贸零售/酒旅: 62.5% red (>3x market), 3 limit-ups, avg > +1.6%, flat-open core 王府井
    _add_stock("600859", "SH", "王府井", "百货", -0.4, 2.9, 3.6e8, rps=62.0, l2_el=True, chs=["地量蓄势"])
    for i in range(2):
        _add_stock(f"6018{i:02d}", "SH", f"百货板{i}", "百货", 0.4, 10.0, 2.6e8)
    for i in range(2):
        _add_stock(f"6019{i:02d}", "SH", f"百货红{i}", "百货", 0.2, 1.4, 2.1e8)
    for i in range(4):
        _add_stock(f"0029{i:02d}", "SZ", f"百货绿{i}", "百货", -0.8, -0.6, 1.5e8)
    _add_stock("601933", "SH", "永辉板", "超市连锁", 0.4, 10.0, 2.6e8)
    for i in range(2):
        _add_stock(f"6031{i:02d}", "SH", f"超市红{i}", "超市连锁", 0.2, 1.6, 2.1e8)
    _add_stock("603199", "SH", "超市绿", "超市连锁", -0.5, -0.4, 1.5e8)
    for i in range(2):
        _add_stock(f"6010{i:02d}", "SH", f"酒旅红{i}", "酒店餐饮", -0.1, 1.5, 2.0e8)
    _add_stock("601099", "SH", "酒旅绿", "酒店餐饮", -0.6, -0.5, 1.5e8)

    # A) 09:26 call auction frame -> 09:25 竞价主线雷达 ranks 大医药 ahead of bleeding 半导体, flags gap-up 生物制药
    lvl_26, _, body_26, _, _ = radar.analyze_snapshot_diff(
        quotes, {}, l2_map, {}, now_ts=ts_0926, turnover_dir=tmp_path / "to_26"
    )
    assert lvl_26 == "L1"
    assert "09:25 竞价主线雷达" in body_26
    assert "极端低开恐慌(仅关注平开中军·禁追高开>1.5%)" in body_26
    assert "大医药" in body_26
    assert "生物制药(红开100%⚠️高开防兑现)" in body_26
    assert "最弱失血：泛AI/半导体算力" in body_26

    # B) 09:50 frame -> L2 mainline_formed for both 大医药 & 商贸零售/酒旅; keeps flat cores, blocks +2.5% gap-up trap
    lvl_50, title_50, body_50, _, cds_50 = radar.analyze_snapshot_diff(
        quotes, {}, l2_map, {}, now_ts=ts_0950, turnover_dir=tmp_path / "to_50"
    )
    assert lvl_50 == "L2"
    assert "【L2 加急焦点】" in title_50
    assert "【主线成型·中军战法】大医药" in body_50
    assert "【主线成型·中军战法】商贸零售/酒旅" in body_50
    assert "恐慌日逆势平开中军观察" in body_50
    assert "复星医药" in body_50 and "大参林" in body_50 and "王府井" in body_50
    assert "高开生物陷" not in body_50.split("5. 全市场")[0]
    assert "高开化药陷" not in body_50.split("5. 全市场")[0]
    assert cds_50.get("L2:mainline_formed:大医药") == ts_0950
    assert cds_50.get("L2:mainline_formed:商贸零售/酒旅") == ts_0950

    rs_map = {r["symbol"]: r for r in tc.rank_intraday_rs_leaders(quotes, l2_map, top_n=6)}
    assert "600196.SH" in rs_map and "603233.SH" in rs_map and "600859.SH" in rs_map
    assert "300122.SZ" not in rs_map and "600999.SH" not in rs_map

    # C) 10:35 frame -> outside 09:25~10:15 window, 09:25 竞价主线雷达 disappears
    _, _, body_35, _, _ = radar.analyze_snapshot_diff(
        quotes, {}, l2_map, {}, now_ts=ts_1035, turnover_dir=tmp_path / "to_35"
    )
    assert "09:25 竞价主线雷达" not in body_35


def test_trajectory_stealth_climb_and_index_dip_resilience_l2_flow(tmp_path) -> None:
    radar = _load_radar_module()
    today_d = cn_today()
    today_s = today_d.isoformat()
    ts_0955 = datetime(today_d.year, today_d.month, today_d.day, 9, 55, tzinfo=CN_TZ).timestamp()

    # Market drops -0.8% across 09:35 -> 09:55 while 2 化学制药 L2 cores steadily climb +0.5%/frame (never >=2.5%/5m)
    history = []
    hist_steps = [
        (-1200.0, 0.2, 0.2, 0.40, 1.2, 0.30, 1.0),
        (-900.0, 0.0, -0.1, 0.90, 1.9, 0.80, 1.5),
        (-600.0, -0.4, -0.5, 1.40, 2.7, 1.30, 2.1),
        (-300.0, -0.7, -0.8, 1.90, 3.6, 1.80, 2.7),
    ]
    for off_s, mkt_p, cy_p, fx_p, fx_a, yl_p, yl_a in hist_steps:
        q_hist = {
            "600196.SH": [fx_p, fx_a, 0.6],
            "002603.SZ": [yl_p, yl_a, 0.7],
        }
        for i in range(25):
            q_hist[f"688{i:03d}.SH"] = [mkt_p, 1.0 + i * 0.05, -0.3]
        history.append({"ts": ts_0955 + off_s, "sh_pct": mkt_p, "cy_pct": cy_p, "med_pct": mkt_p, "q": q_hist})

    prev_state = {
        "date": today_s,
        "ts": ts_0955 - 300.0,
        "red": 5,
        "cooldowns": {},
        "quotes": {k: {"pct": v[0], "amt_yi": v[1]} for k, v in history[-1]["q"].items()},
        "history": history,
    }
    l2_map = {
        "600196": {
            "name": "复星医药",
            "sector": "化学制药",
            "rps": 76.0,
            "l2_eligible": True,
            "channels": ["主升通道"],
        },
        "002603": {
            "name": "以岭药业",
            "sector": "化学制药",
            "rps": 68.0,
            "l2_eligible": True,
            "channels": ["地量蓄势"],
        },
    }
    curr_quotes = {
        "000001.SH": {
            "symbol": "000001.SH",
            "prev_close": 3500.0,
            "open": 3507.0,
            "last_price": 3479.0,
            "amount": 2.0e11,
        },
        "399001.SZ": {
            "symbol": "399001.SZ",
            "prev_close": 10000.0,
            "open": 10020.0,
            "last_price": 9920.0,
            "amount": 2.4e11,
        },
        "399006.SZ": {
            "symbol": "399006.SZ",
            "prev_close": 2200.0,
            "open": 2205.0,
            "last_price": 2180.0,
            "amount": 8.0e10,
        },
        "600196.SH": {
            "symbol": "600196.SH",
            "prev_close": 20.0,
            "open": 20.02,
            "last_price": 20.48,  # +2.40%, even sitting in fuzzy VWAP band +2.32% (> old +2.2% cliff!)
            "high": 20.52,
            "low": 20.0,
            "vwap": 20.015,
            "amount": 4.6e8,
            "ext": {"name": "复星医药"},
        },
        "002603.SZ": {
            "symbol": "002603.SZ",
            "prev_close": 20.0,
            "open": 20.04,
            "last_price": 20.46,  # +2.30%, +0.5% 5m step, steady VWAP +1.1%
            "high": 20.50,
            "low": 20.0,
            "vwap": 20.237,
            "amount": 3.4e8,
            "ext": {"name": "以岭药业"},
        },
    }
    for i in range(25):
        sym = f"688{i:03d}.SH"
        l2_map[sym[:6]] = {"name": f"科技{i}", "sector": "半导体", "rps": 50.0}
        curr_quotes[sym] = {
            "symbol": sym,
            "prev_close": 10.0,
            "open": 10.0,
            "last_price": 9.91,
            "high": 10.02,
            "low": 9.88,
            "vwap": 9.95,
            "amount": 1.2e8,
            "ext": {"name": f"科技{i}"},
        }

    lvl, title, body, _, cds = radar.analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, {}, now_ts=ts_0955, turnover_dir=tmp_path / "to_flow"
    )
    assert lvl == "L2"
    assert "【L2 加急焦点】" in title
    assert "【分时势能成型·绕过点位慢推/抗跌】大医药" in body
    assert "分时势能与抗跌阻尼榜" in body
    assert "📈压速慢推" in body
    assert "🛡️抗跌阻尼" in body
    assert "复星医药" in body and "以岭药业" in body
    assert cds.get("L2:flow_trend:大医药") == ts_0955


def test_holding_hairline_vwap_poke_waits_for_second_frame_confirmation(tmp_path) -> None:
    radar = _load_radar_module()
    today_d = cn_today()
    today_s = today_d.isoformat()
    ts_1005 = datetime(today_d.year, today_d.month, today_d.day, 10, 5, tzinfo=CN_TZ).timestamp()
    ts_1010 = datetime(today_d.year, today_d.month, today_d.day, 10, 10, tzinfo=CN_TZ).timestamp()
    holdings = radar.parse_holdings_arg(["603011.SH:14.00:13.00:ATK"])

    # Frame 1: prior frame above VWAP (+0.5%); current frame hairline poke (-0.20%) below VWAP -> L1 watch only
    q_poke = {
        "603011.SH": {
            "symbol": "603011.SH",
            "prev_close": 14.34,
            "open": 14.94,
            "last_price": 14.97,
            "high": 15.25,  # high_pct=+6.35%, upper_wick_pct=1.84%
            "low": 14.92,
            "volume": 2.9e8 / 15.00,  # amt / vol = 15.00 -> vwap_dev = -0.20% (> -0.45%)
            "amount": 2.9e8,
            "ext": {"name": "合锻智能"},
        }
    }
    prev_above = {
        "date": today_s,
        "ts": ts_1005 - 300.0,
        "red": 0,
        "cooldowns": {},
        "quotes": {"603011.SH": {"pct": 4.6, "amt_yi": 2.5}},
        "history": [
            {"ts": ts_1005 - 300.0, "sh_pct": 0.0, "cy_pct": 0.0, "med_pct": 0.0, "q": {"603011.SH": [4.6, 2.5, 0.50]}}
        ],
    }
    lvl1, _, body1, _, cds1 = radar.analyze_snapshot_diff(
        q_poke, prev_above, {}, holdings, now_ts=ts_1005, turnover_dir=tmp_path / "to_hk1"
    )
    assert lvl1 == "L1"
    assert "👀微破均线观察(防刺破洗盘" in body1
    assert "L2:holding_rollover:603011.SH" not in cds1

    # Frame 2A: reclaim back above VWAP (+0.33%) -> stays L1
    q_reclaim = {"603011.SH": {**q_poke["603011.SH"], "last_price": 15.05, "amount": 3.0e8, "volume": 3.0e8 / 15.00}}
    prev_under = {
        "date": today_s,
        "ts": ts_1005,
        "red": 0,
        "cooldowns": cds1,
        "quotes": {"603011.SH": {"pct": 4.39, "amt_yi": 2.9}},
        "history": [
            {"ts": ts_1005, "sh_pct": 0.0, "cy_pct": 0.0, "med_pct": 0.0, "q": {"603011.SH": [4.39, 2.9, -0.20]}}
        ],
    }
    lvl_rec, _, _, _, cds_rec = radar.analyze_snapshot_diff(
        q_reclaim, prev_under, {}, holdings, now_ts=ts_1010, turnover_dir=tmp_path / "to_hk2"
    )
    assert lvl_rec == "L1"
    assert "L2:holding_rollover:603011.SH" not in cds_rec

    # Frame 2B: 2nd consecutive frame remains below VWAP (-0.22%) -> confirmed L2 rollover profit lock!
    q_confirm = {"603011.SH": {**q_poke["603011.SH"], "last_price": 14.967, "amount": 3.0e8, "volume": 3.0e8 / 15.00}}
    lvl2, _, body2, _, cds2 = radar.analyze_snapshot_diff(
        q_confirm, prev_under, {}, holdings, now_ts=ts_1010, turnover_dir=tmp_path / "to_hk3"
    )
    assert lvl2 == "L2"
    assert "【老仓冲高破均线锁利】" in body2
    assert cds2.get("L2:holding_rollover:603011.SH") == ts_1010


def test_shallow_lps_pullback_and_near_miss_mainline_cliff_removal(tmp_path) -> None:
    import sys

    sys.path.insert(0, str(SCRIPT_PATH.parent))
    import telegraph_catalysts as tc

    radar = _load_radar_module()
    yesterday_s = (cn_today() - timedelta(days=1)).isoformat()
    today_s = cn_today().isoformat()
    anchors = tmp_path / "anchors.json"
    anchors.write_text(
        json.dumps(
            {
                "trade_date": yesterday_s,
                "anchors": [{"symbol": "600196.SH", "name": "复星医药", "kind": "L2中军", "theme": "化学制药"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    q_lps = {
        "600196.SH": {
            "symbol": "600196.SH",
            "prev_close": 20.0,
            "open": 20.10,
            "last_price": 20.33,  # +1.65%
            "volume": 3.0e8 / 20.00,  # amt / vol = 20.00 -> vwap_dev = +1.65% (shallow pullback 1.3 < 1.65 <= 1.85)
            "amount": 3.0e8,
            "ext": {"name": "复星医药"},
        }
    }
    lps_md = radar.scan_next_day_lps_pullbacks(
        today_s,
        "10:05",
        q_lps,
        {"600196.SH": {"pct": 1.35, "amt_yi": 2.4}},
        True,
        0.55,
        anchors_path=anchors,
    )
    assert "🎯浅回踩抢筹(未摸均线即托住)" in lps_md

    # Near-miss mainline confirmation: 12 red / 16 total = 75.0% (< 78% old cliff), 4 limit-ups, 42亿 amt
    l2_map, m_quotes = {}, {}
    for i in range(16):
        code = f"0025{i:02d}"
        pct = 10.0 if i < 4 else (1.8 if i < 12 else -0.5)
        l2_map[code] = {"name": f"药{i}", "sector": "化学制药", "rps": 55.0, "l2_eligible": (i == 4)}
        m_quotes[f"{code}.SZ"] = {
            "symbol": f"{code}.SZ",
            "prev_close": 10.0,
            "open": 10.0,
            "last_price": round(10.0 * (1.0 + pct / 100.0), 3),
            "high": round(10.0 * (1.0 + max(pct, 0.2) / 100.0), 3),
            "low": 9.95,
            "vwap": 10.05 if pct > 0 else 10.02,
            "amount": 2.7e8,
            "ext": {"name": f"药{i}"},
        }
    alerts = tc.detect_confirmed_mainline_alerts(m_quotes, l2_map)
    assert any(th == "大医药" for th, _, _ in alerts)
