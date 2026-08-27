from datetime import date

import pandas as pd

from core.shadow_ledger import (
    ShadowBook,
    ShadowPlan,
    ShadowPosition,
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


def test_next_open_fill_requires_prior_day_plan_and_uses_official_open() -> None:
    as_of = date(2026, 8, 20)
    plan = ShadowPlan(
        plan_key=plan_key("USER_SHADOW:test", as_of, "buy", "000001"),
        code="000001",
        name="平安银行",
        action="buy",
        signal_date=as_of,
        suggested_price=10.0,
        shares_hint=1000,
        reason="confirmed",
    )
    same_day = try_fill_plan(ShadowBook(), plan, _bars(), as_of)
    assert same_day is not None
    assert same_day.status == "skipped"
    assert same_day.fill_reason == "lookahead_blocked"

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
