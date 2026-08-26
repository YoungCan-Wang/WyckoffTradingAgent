from datetime import date

import pandas as pd

from core.shadow_ledger import ShadowBook, ShadowPlan, plan_key, run_shadow_session, try_fill_plan
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


def test_shadow_account_guard_rejects_user_live() -> None:
    try:
        assert_shadow_account("USER_LIVE:e66942b7-be66-46fe-95ed-ebc7f3b47928")
    except ValueError as exc:
        assert "USER_SHADOW" in str(exc)
    else:
        raise AssertionError("USER_LIVE must be rejected")
    assert assert_shadow_account("USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928")
