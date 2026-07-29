"""未执行离场工单的识别边界。"""

from __future__ import annotations

from core.execution_audit import (
    find_unexecuted_exits,
    render_stale_exit_alert,
    stop_breached_codes,
    unsellable_dates,
)


def _order(code: str, date: str, action: str = "EXIT", status: str = "APPROVED", name: str = "") -> dict:
    return {"code": code, "name": name or code, "action": action, "status": status, "trade_date": date}


def test_repeated_exit_on_a_still_held_position_is_flagged():
    orders = [_order("603661", d, name="恒林股份") for d in ("20260713", "20260714", "20260715")]

    stale = find_unexecuted_exits(orders, ["603661"])

    assert [(s.code, s.days, s.since) for s in stale] == [("603661", 3, "20260713")]
    assert stale[0].is_severe


def test_a_single_day_exit_is_not_yet_a_problem():
    assert find_unexecuted_exits([_order("603661", "20260715")], ["603661"]) == []


def test_exit_for_a_code_no_longer_held_means_it_was_executed():
    orders = [_order("603661", d) for d in ("20260713", "20260714")]

    assert find_unexecuted_exits(orders, ["600000"]) == []


def test_sealed_limit_down_days_do_not_count_as_procrastination():
    """一字跌停当天卖不掉，不该记在执行纪律头上。"""
    orders = [_order("603661", d) for d in ("20260713", "20260714", "20260715")]

    stale = find_unexecuted_exits(orders, ["603661"], unsellable_by_code={"603661": {"20260714", "20260715"}})

    assert stale == []


def test_a_sealed_day_in_the_middle_does_not_wipe_out_earlier_delay():
    orders = [_order("603661", d) for d in ("20260713", "20260714", "20260715")]

    stale = find_unexecuted_exits(orders, ["603661"], unsellable_by_code={"603661": {"20260714"}})

    assert [(s.code, s.days, s.since) for s in stale] == [("603661", 2, "20260713")]


def test_closing_at_the_limit_is_sellable_if_it_traded_higher_intraday():
    """生产实例：昊华 07-20 收于跌停 42.19，但盘中最高 48.65，全天有卖出窗口。"""
    bars = [
        ("2026-07-20", 46.88, 48.65, 42.19),  # 收跌停但盘中可卖
        ("2026-07-21", 42.19, 37.97, 37.97),  # 一字跌停
    ]

    assert unsellable_dates(bars) == {"2026-07-21"}


def test_normal_down_day_is_never_treated_as_unsellable():
    assert unsellable_dates([("2026-07-17", 31.74, 30.50, 28.60)]) == set()


def test_only_positions_below_their_stop_freeze_new_buys():
    """没落袋的止盈拖着只是少赚，不该冻结新仓；跌破止损才是风控失效。"""
    orders = [_order(code, d) for code in ("603661", "600611") for d in ("20260727", "20260728")]

    stale = find_unexecuted_exits(orders, ["603661", "600611"])
    blocking = stop_breached_codes(
        stale,
        {"603661": 36.73, "600611": 3.77},
        {"603661": 27.83, "600611": 4.02},
    )

    assert {s.code for s in stale} == {"603661", "600611"}
    assert blocking == frozenset({"603661"})


def test_a_position_without_a_stop_cannot_be_judged_breached():
    orders = [_order("603661", d) for d in ("20260727", "20260728")]
    stale = find_unexecuted_exits(orders, ["603661"])

    assert stop_breached_codes(stale, {"603661": None}, {"603661": 27.83}) == frozenset()


def test_streak_only_counts_back_to_the_first_gap():
    orders = [_order("603661", d) for d in ("20260713", "20260716", "20260717")]
    # 20260714/15 这两天 OMS 跑过但没建议离场，说明中间恢复过，连续段只到 0716。
    orders += [_order("600000", d) for d in ("20260714", "20260715")]

    stale = find_unexecuted_exits(orders, ["603661", "600000"])

    assert [(s.code, s.days) for s in stale] == [("603661", 2)]


def test_a_skipped_run_day_does_not_reset_the_streak():
    """漏跑一天不该把告警清零：连续性按实际运行日算，不按自然日。"""
    orders = [_order("603661", d) for d in ("20260713", "20260716", "20260717")]

    stale = find_unexecuted_exits(orders, ["603661"])

    assert stale[0].days == 3


def test_cancelled_and_no_trade_orders_are_ignored():
    orders = [
        _order("603661", "20260713", status="CANCELLED"),
        _order("603661", "20260714", status="NO_TRADE"),
        _order("603661", "20260715"),
    ]

    assert find_unexecuted_exits(orders, ["603661"]) == []


def test_buy_side_actions_never_count_as_stale_exits():
    orders = [_order("603661", d, action="PROBE") for d in ("20260713", "20260714")]

    assert find_unexecuted_exits(orders, ["603661"]) == []


def test_trim_counts_alongside_exit():
    orders = [_order("600611", d, action="TRIM") for d in ("20260713", "20260714")]

    stale = find_unexecuted_exits(orders, ["600611"])

    assert [(s.code, s.action) for s in stale] == [("600611", "TRIM")]


def test_alert_is_silent_when_nothing_is_stale():
    assert render_stale_exit_alert([]) == []


def test_alert_escalates_wording_past_three_days():
    orders = [_order("603661", d, name="恒林股份") for d in ("20260713", "20260714", "20260715")]

    text = "\n".join(render_stale_exit_alert(find_unexecuted_exits(orders, ["603661"])))

    assert "🚨" in text
    assert "恒林股份(603661)" in text
    assert "连续建议 3 个交易日" in text
    assert "portfolio fill" in text
