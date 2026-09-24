from datetime import date

import pandas as pd

from core.shadow_ledger import (
    ShadowBook,
    ShadowPlan,
    ShadowPosition,
    event_key,
    plan_key,
    run_shadow_session,
    try_fill_plan,
)
from integrations.supabase_shadow import assert_shadow_account


def _bars() -> dict[str, pd.DataFrame]:
    return {
        "000001": pd.DataFrame(
            {
                "date": [date(2026, 8, 20), date(2026, 8, 21)],
                "open": [9.80, 10.50],
                "high": [10.20, 11.00],
                "low": [9.70, 10.40],
                "close": [10.00, 10.90],
            }
        )
    }


def _buy_plan(as_of: date) -> ShadowPlan:
    return ShadowPlan(
        plan_key=plan_key("USER_SHADOW:test", as_of, "buy", "000001"),
        code="000001",
        name="平安银行",
        action="buy",
        signal_date=as_of,
        suggested_price=10.0,
        shares_hint=1000,
        reason="confirmed",
    )


def test_next_open_fill_requires_prior_day_plan_and_uses_official_open() -> None:
    as_of = date(2026, 8, 20)
    plan = _buy_plan(as_of)
    # 信号日当天不可成交：返回 None，保留 planned，避免同日重跑写成 skipped 落库。
    assert try_fill_plan(ShadowBook(), plan, _bars(), as_of) is None

    session = run_shadow_session(
        ShadowBook(),
        [plan],
        [],
        _bars(),
        date(2026, 8, 21),
        account_id="USER_SHADOW:test",
        allow_new_buys=False,
    )
    filled = session.fills[0]
    assert filled.status == "filled"
    assert filled.entry_date == date(2026, 8, 21)
    assert filled.entry_price == 10.50
    assert filled.qty >= 100
    assert session.book.positions["000001"].sellable_shares == 0


def test_same_day_rerun_keeps_tonight_plans_unfilled() -> None:
    """同日第二次跑会话不得把今夜计划放进 fills（否则 upsert 会改成 skipped）。"""
    as_of = date(2026, 8, 20)
    plan = _buy_plan(as_of)
    session = run_shadow_session(
        ShadowBook(),
        [plan],
        [],
        _bars(),
        as_of,
        account_id="USER_SHADOW:test",
        allow_new_buys=False,
    )
    assert session.fills == []
    assert plan.status == "planned"


def test_missing_as_of_bar_keeps_plan_planned() -> None:
    """缺当日开盘价不得写成 skipped——否则计划被 upsert 后永远不会再兑现。"""
    plan = ShadowPlan(
        plan_key=plan_key("USER_SHADOW:test", date(2026, 8, 20), "buy", "000001"),
        code="000001",
        name="平安银行",
        action="buy",
        signal_date=date(2026, 8, 20),
        suggested_price=10.0,
        shares_hint=1000,
    )
    bars = {
        "000001": pd.DataFrame(
            {
                "date": [date(2026, 8, 19), date(2026, 8, 20)],
                "open": [9.50, 9.80],
                "high": [10.00, 10.20],
                "low": [9.40, 9.70],
                "close": [9.80, 10.00],
            }
        )
    }
    assert try_fill_plan(ShadowBook(), plan, bars, date(2026, 8, 21)) is None


def test_sells_fill_before_buys_so_rotation_can_reuse_cash() -> None:
    """止损卖与新买同日到期时，必须先卖后买；buy 字典序小于 sell，按 plan_key 序会毁单。"""
    as_of = date(2026, 8, 21)
    book = ShadowBook(cash=50.0)
    book.positions["000002"] = ShadowPosition(
        code="000002",
        name="旧仓",
        shares=1000,
        sellable_shares=1000,
        avg_cost=10.0,
        buy_dt=date(2026, 8, 1),
        last_mark=50.0,
    )
    bars = {
        "000001": pd.DataFrame({"date": [as_of], "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5]}),
        "000002": pd.DataFrame({"date": [as_of], "open": [50.0], "high": [51.0], "low": [49.0], "close": [50.0]}),
    }
    buy = ShadowPlan(
        plan_key=plan_key("USER_SHADOW:test", date(2026, 8, 20), "buy", "000001"),
        code="000001",
        name="新票",
        action="buy",
        signal_date=date(2026, 8, 20),
        shares_hint=100,
        suggested_price=10.0,
    )
    sell = ShadowPlan(
        plan_key=plan_key("USER_SHADOW:test", date(2026, 8, 20), "sell", "000002"),
        code="000002",
        name="旧仓",
        action="sell",
        signal_date=date(2026, 8, 20),
        shares_hint=1000,
        suggested_price=50.0,
    )
    # 故意把 buy 放前面，模拟 plan_key / 主键序
    session = run_shadow_session(
        book,
        [buy, sell],
        [],
        bars,
        as_of,
        account_id="USER_SHADOW:test",
        allow_new_buys=False,
    )
    assert [p.action for p in session.fills] == ["sell", "buy"]
    assert all(p.status == "filled" for p in session.fills)
    assert "000002" not in session.book.positions
    assert session.book.positions["000001"].shares >= 100


def test_shadow_account_guard_rejects_user_live() -> None:
    try:
        assert_shadow_account("USER_LIVE:e66942b7-be66-46fe-95ed-ebc7f3b47928")
    except ValueError as exc:
        assert "USER_SHADOW" in str(exc)
    else:
        raise AssertionError("USER_LIVE must be rejected")
    assert assert_shadow_account("USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928")


def test_event_key_keeps_buy_and_sell_distinct() -> None:
    """event_key 必须含 action：同日同股同量的买/卖不能共用一个幂等键。"""
    as_of = date(2026, 8, 27)
    buy = event_key("USER_SHADOW:test", as_of, "buy", "600519", 100, status="filled")
    sell = event_key("USER_SHADOW:test", as_of, "sell", "600519", 100, status="filled")
    assert buy != sell
    assert ":buy:" in buy and ":sell:" in sell
    # 旧写法用 status 当 type 时两者撞成同一键
    legacy = f"USER_SHADOW:test:{as_of.isoformat()}:filled:600519:100"
    assert buy != legacy and sell != legacy


def test_event_key_distinguishes_skipped_from_filled() -> None:
    as_of = date(2026, 8, 27)
    skipped = event_key("USER_SHADOW:test", as_of, "buy", "000001", 0, status="skipped")
    filled = event_key("USER_SHADOW:test", as_of, "buy", "000001", 100, status="filled")
    assert skipped != filled
    assert skipped.endswith(":skipped")
    assert filled.endswith(":filled")


def _ohlc_bars(codes: list[str], days: list[date]) -> dict[str, pd.DataFrame]:
    return {
        code: pd.DataFrame(
            {
                "date": list(days),
                "open": [10.0] * len(days),
                "high": [11.0] * len(days),
                "low": [9.0] * len(days),
                "close": [10.0] * len(days),
            }
        )
        for code in codes
    }


def test_pending_no_open_buys_reserve_slots_so_max_positions_holds() -> None:
    """部分计划因缺 K 线保持 planned 时，不得再占满剩余槽；否则次日一并成交会突破 4 仓。"""
    import copy

    from core.shadow_ledger import MAX_POSITIONS

    t0, t1, t2 = date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)
    first4 = [f"00000{i}" for i in range(1, 5)]
    extras = [f"00000{i}" for i in range(5, 9)]
    all_codes = first4 + extras
    cands = [{"code": code, "name": code, "tag": "confirmed"} for code in all_codes]

    day0 = run_shadow_session(ShadowBook(), [], cands, _ohlc_bars(all_codes, [t0]), t0, account_id="USER_SHADOW:test")
    assert [plan.code for plan in day0.new_plans] == first4

    # 001/002 与新候选有 K 线可成交/可提案；003/004 缺线 → no_open 仍 planned。
    have_t1 = ["000001", "000002", *extras]
    day1 = run_shadow_session(
        ShadowBook(),
        day0.new_plans,
        cands,
        _ohlc_bars(have_t1, [t0, t1]),
        t1,
        account_id="USER_SHADOW:test",
    )
    assert sorted(p.code for p in day1.fills if p.status == "filled") == ["000001", "000002"]
    assert day1.new_plans == []

    filled = {plan.code for plan in day1.fills}
    pending = [plan for plan in day0.new_plans if plan.code not in filled] + list(day1.new_plans)
    day2 = run_shadow_session(
        copy.deepcopy(day1.book),
        pending,
        [],
        _ohlc_bars(all_codes, [t0, t1, t2]),
        t2,
        account_id="USER_SHADOW:test",
        allow_new_buys=False,
    )
    assert len(day2.book.positions) <= MAX_POSITIONS
    assert sorted(day2.book.positions) == ["000001", "000002", "000003", "000004"]


def test_pending_no_open_buy_blocks_same_code_repropose() -> None:
    """同票 open 缺失但 close 仍在时，不得再写一条 planned，否则次日双倍加仓。"""
    t0, t1, t2 = date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)
    day0 = run_shadow_session(
        ShadowBook(),
        [],
        [{"code": "000001", "name": "x", "tag": "t"}],
        _ohlc_bars(["000001"], [t0]),
        t0,
        account_id="USER_SHADOW:test",
    )
    plan = day0.new_plans[0]
    bars_bad_open = {
        "000001": pd.DataFrame(
            {
                "date": [t0, t1],
                "open": [10.0, 0.0],
                "high": [11.0, 11.0],
                "low": [9.0, 9.0],
                "close": [10.0, 10.0],
            }
        )
    }
    day1 = run_shadow_session(
        ShadowBook(),
        [plan],
        [{"code": "000001", "name": "x", "tag": "t"}],
        bars_bad_open,
        t1,
        account_id="USER_SHADOW:test",
    )
    assert day1.fills == []
    assert day1.new_plans == []

    day2 = run_shadow_session(
        ShadowBook(),
        [plan],
        [],
        _ohlc_bars(["000001"], [t0, t1, t2]),
        t2,
        account_id="USER_SHADOW:test",
        allow_new_buys=False,
    )
    assert len(day2.fills) == 1
    assert day2.book.positions["000001"].shares == 2400
