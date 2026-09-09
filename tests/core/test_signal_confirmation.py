from __future__ import annotations

import pandas as pd

from core.signal_confirmation import (
    check_confirmation,
    elapsed_trade_days,
    market_trade_calendar,
    run_confirmation_cycle,
)


def test_sos_confirmation_requires_reclaiming_signal_close():
    snap = {"snap_low": 9.2, "snap_close": 10.0, "snap_volume": 1_000_000}

    status, reason = check_confirmation(
        "sos",
        snap,
        {"low": 9.4, "close": 9.8, "volume": 700_000},
        days_elapsed=1,
    )

    assert status == "survived"
    assert "等待缩量" in reason


def test_sos_confirmation_accepts_shrinkage_above_signal_close():
    snap = {"snap_low": 9.2, "snap_close": 10.0, "snap_volume": 1_000_000}

    status, reason = check_confirmation(
        "sos",
        snap,
        {"low": 9.4, "close": 10.05, "volume": 700_000},
        days_elapsed=1,
    )

    assert status == "confirmed"
    assert "信号日收盘" in reason


def test_sos_confirmation_rejects_close_below_ma20():
    snap = {"snap_low": 9.2, "snap_close": 10.0, "snap_volume": 1_000_000}

    status, reason = check_confirmation(
        "sos",
        snap,
        {"low": 9.4, "close": 10.05, "volume": 700_000, "ma20": 10.5},
        days_elapsed=1,
    )

    assert status == "survived"
    assert "站稳MA20" in reason


def test_evr_confirmation_rejects_close_below_ma20():
    snap = {"snap_support": 9.5, "snap_close": 9.8}

    status, reason = check_confirmation(
        "evr",
        snap,
        {"low": 9.4, "close": 9.6, "volume": 500_000, "ma20": 10.2},
        days_elapsed=1,
    )

    assert status == "survived"
    assert "站稳MA20" in reason


def test_lps_weak_close_only_survives_without_validation():
    status, reason = check_confirmation(
        "lps",
        {"snap_support": 10.0, "snap_close": 10.3, "snap_volume": 1_000_000, "snap_ma20": 10.0},
        {"open": 10.3, "high": 10.4, "low": 10.0, "close": 10.05, "volume": 700_000, "ma20": 10.0},
        days_elapsed=1,
    )

    assert status == "survived"
    assert "高收" in reason


def test_lps_high_close_and_dry_volume_validates_demand():
    status, reason = check_confirmation(
        "lps",
        {"snap_support": 10.0, "snap_close": 10.3, "snap_volume": 1_000_000, "snap_ma20": 10.0},
        {"open": 10.2, "high": 10.6, "low": 10.0, "close": 10.5, "volume": 700_000, "ma20": 10.1},
        days_elapsed=1,
    )

    assert status == "confirmed"
    assert "需求确认" in reason


def test_lps_one_price_up_day_can_validate_research_signal():
    status, reason = check_confirmation(
        "lps",
        {"snap_support": 10.0, "snap_close": 10.3, "snap_volume": 1_000_000, "snap_ma20": 10.0},
        {"open": 11.3, "high": 11.3, "low": 11.3, "close": 11.3, "volume": 800_000, "ma20": 10.2},
        days_elapsed=1,
    )

    assert status == "confirmed"
    assert "需求确认" in reason


def test_confirmation_cycle_marks_confirmed_source_for_step3():
    df = pd.DataFrame(
        [
            {"date": "2026-06-11", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000},
            {"date": "2026-06-12", "open": 10.3, "high": 10.8, "low": 10.1, "close": 10.7, "volume": 900},
        ]
    )

    updates, confirmed = run_confirmation_cycle(
        [
            {
                "id": 1,
                "code": 1,
                "name": "平安银行",
                "signal_type": "evr",
                "signal_date": "2026-06-11",
                "signal_score": 1.2,
                "days_elapsed": 0,
                "snap_support": 10.0,
                "snap_close": 10.2,
            }
        ],
        {"000001": df},
        "2026-06-12",
    )

    assert updates[0]["status"] == "confirmed"
    assert confirmed[0]["selection_source"] == "signal_confirmed"
    assert confirmed[0]["source_type"] == "signal_pending"
    assert confirmed[0]["confirm_date"] == "2026-06-12"
    assert confirmed[0]["confirm_reason"]


def _bars(dates: list[str], close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": d, "open": close, "high": close + 0.5, "low": close - 0.4, "close": close, "volume": 900}
            for d in dates
        ]
    )


def _sos_signal(signal_date: str = "2026-06-11") -> dict:
    """一个不会被确认谓词提前打死、只会走到 TTL 的 sos 信号(ttl=2)。"""
    return {
        "id": 1,
        "code": 1,
        "name": "平安银行",
        "signal_type": "sos",
        "signal_date": signal_date,
        "signal_score": 1.2,
        "days_elapsed": 0,
        "snap_low": 9.2,
        "snap_close": 10.6,
        "snap_volume": 1_000_000,
    }


def test_same_trade_date_rerun_does_not_burn_ttl():
    """同一个 trade_date 跑两轮,days_elapsed 不能翻倍。

    线上 08-24/08-27/08-30 各跑了两轮,53 个信号因此提前一个交易日 expired。
    """
    df = _bars(["2026-06-11", "2026-06-12"])

    first, _ = run_confirmation_cycle([_sos_signal()], {"000001": df}, "2026-06-12")
    # 第二轮拿到的是第一轮写回后的 days_elapsed
    sig = _sos_signal()
    sig["days_elapsed"] = first[0]["days_elapsed"]
    second, _ = run_confirmation_cycle([sig], {"000001": df}, "2026-06-12")

    assert first[0]["days_elapsed"] == 1
    assert second[0]["days_elapsed"] == 1, "重跑同一天不应把 days_elapsed 推到 2"
    assert second[0]["status"] != "expired", "sos 的 ttl=2,第一个交易日就不该到期"


def test_days_elapsed_counts_trade_days_not_calls():
    """跳过一轮跑批后,days_elapsed 要按真实经过的交易日跳,不是按调用次数 +1。"""
    df = _bars(["2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16"])

    updates, _ = run_confirmation_cycle([_sos_signal()], {"000001": df}, "2026-06-16")

    assert updates[0]["days_elapsed"] == 3
    assert updates[0]["status"] == "expired"


def test_suspended_stock_still_expires_on_ttl():
    """停牌股拿不到当日 bar,但 TTL 照走,不能永远挂在 pending 池里。"""
    stale = _bars(["2026-06-11", "2026-06-12"])  # 12 日之后停牌
    calendar_peer = _bars(["2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16"])

    updates, confirmed = run_confirmation_cycle(
        [_sos_signal()],
        {"000001": stale, "000002": calendar_peer},
        "2026-06-16",
    )

    assert not confirmed
    assert len(updates) == 1
    assert updates[0]["status"] == "expired"
    assert updates[0]["days_elapsed"] == 3
    assert "无可用K线" in updates[0]["confirm_reason"]


def test_stale_bar_is_not_treated_as_today():
    """停牌股的旧 bar 不能当"今天"用来判确认——否则用过期价格决定信号生死。"""
    # 这根旧 bar 单看是满足 sos 确认的(收在信号日收盘之上 + 缩量)
    stale = pd.DataFrame(
        [
            {"date": "2026-06-11", "open": 10.0, "high": 10.8, "low": 9.4, "close": 10.6, "volume": 1_000_000},
            {"date": "2026-06-12", "open": 10.6, "high": 11.0, "low": 10.4, "close": 10.9, "volume": 600_000},
        ]
    )
    peer = _bars(["2026-06-11", "2026-06-12", "2026-06-15"])

    _, confirmed = run_confirmation_cycle(
        [_sos_signal()],
        {"000001": stale, "000002": peer},
        "2026-06-15",
    )

    assert not confirmed, "06-15 停牌,不该用 06-12 那根 bar 判出 confirmed"


def test_non_trading_date_does_not_age_signals():
    """trade_date 不在全市场日历上时不该老化(周末/节假日误触发)。"""
    df = _bars(["2026-06-11", "2026-06-12"])

    updates, _ = run_confirmation_cycle([_sos_signal()], {"000001": df}, "2026-06-13")

    assert updates == []


def test_calendar_truncates_before_taking_tail():
    """frame 越过 trade_date 时,日历不能被过滤空后退回自然日。

    先取尾再截断的话日历会空,elapsed 退回自然日：06-12→06-15(周一) 算成 3 天,
    sos(ttl=2) 凭空到期。
    """
    df = _bars([f"2026-06-{d:02d}" for d in (11, 12, 15, 16, 17, 18, 19, 22, 23, 24, 25, 26, 29, 30)])

    calendar = market_trade_calendar({"000001": df}, "2026-06-15")

    assert calendar == ["2026-06-11", "2026-06-12", "2026-06-15"]
    assert elapsed_trade_days(calendar, "2026-06-12", "2026-06-15") == 1


def test_future_bar_is_not_treated_as_today():
    """frame 末尾越过 trade_date 时不能拿未来那根当"今天"判确认。"""
    df = _bars(["2026-06-11", "2026-06-12", "2026-06-15", "2026-06-16"])

    _, confirmed = run_confirmation_cycle(
        [_sos_signal(signal_date="2026-06-12")],
        {"000001": df},
        "2026-06-15",
    )

    assert not confirmed
