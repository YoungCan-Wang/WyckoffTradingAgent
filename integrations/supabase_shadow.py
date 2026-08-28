"""影子账本表读写。只碰 shadow_*，禁止写入 USER_LIVE 实盘表。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from core.constants import (
    TABLE_SHADOW_ACCOUNT,
    TABLE_SHADOW_EVENTS,
    TABLE_SHADOW_NAV_DAILY,
    TABLE_SHADOW_POSITIONS,
    TABLE_SHADOW_TRADE_PLANS,
)
from core.shadow_ledger import INITIAL_CAPITAL, SHADOW_ACCOUNT_ID, ShadowBook, ShadowPlan, ShadowPosition
from integrations.supabase_base import create_admin_client as _admin
from integrations.supabase_base import is_admin_configured as _configured
from integrations.supabase_base import require_server_write_context

logger = logging.getLogger(__name__)

_LIVE_TABLES = frozenset({"portfolios", "portfolio_positions", "trade_orders", "daily_nav"})
_SHADOW_TABLES = frozenset(
    {
        TABLE_SHADOW_ACCOUNT,
        TABLE_SHADOW_POSITIONS,
        TABLE_SHADOW_EVENTS,
        TABLE_SHADOW_NAV_DAILY,
        TABLE_SHADOW_TRADE_PLANS,
    }
)


def assert_shadow_account(account_id: str) -> str:
    text = str(account_id or "").strip()
    if not text.startswith("USER_SHADOW:"):
        raise ValueError(f"影子账本拒绝非 USER_SHADOW 账户: {account_id}")
    if text.startswith("USER_LIVE"):
        raise ValueError("影子账本禁止写入 USER_LIVE")
    return text


def _table(name: str):
    if name in _LIVE_TABLES or name not in _SHADOW_TABLES:
        raise ValueError(f"影子账本禁止访问表 {name}")
    require_server_write_context(f"write {name}")
    return _admin().table(name)


def seed_shadow_account(account_id: str = SHADOW_ACCOUNT_ID) -> None:
    if not _configured():
        return
    account_id = assert_shadow_account(account_id)
    existing = _table(TABLE_SHADOW_ACCOUNT).select("account_id").eq("account_id", account_id).limit(1).execute()
    if existing.data:
        return
    _table(TABLE_SHADOW_ACCOUNT).insert(
        {
            "account_id": account_id,
            "cash": INITIAL_CAPITAL,
            "equity": INITIAL_CAPITAL,
            "market_value": 0,
            "initial_capital": INITIAL_CAPITAL,
        }
    ).execute()


def load_shadow_book(account_id: str = SHADOW_ACCOUNT_ID) -> ShadowBook:
    account_id = assert_shadow_account(account_id)
    seed_shadow_account(account_id)
    acc = (_table(TABLE_SHADOW_ACCOUNT).select("*").eq("account_id", account_id).limit(1).execute().data or [{}])[0]
    rows = _table(TABLE_SHADOW_POSITIONS).select("*").eq("account_id", account_id).execute().data or []
    positions = {str(row["code"]): _row_to_position(row) for row in rows if int(row.get("shares") or 0) > 0}
    return ShadowBook(
        cash=float(acc.get("cash") or INITIAL_CAPITAL),
        initial_capital=float(acc.get("initial_capital") or INITIAL_CAPITAL),
        positions=positions,
    )


def load_planned(account_id: str = SHADOW_ACCOUNT_ID) -> list[ShadowPlan]:
    account_id = assert_shadow_account(account_id)
    rows = (
        _table(TABLE_SHADOW_TRADE_PLANS).select("*").eq("account_id", account_id).eq("status", "planned").execute().data
        or []
    )
    return [_row_to_plan(row) for row in rows]


def persist_shadow_session(
    *,
    account_id: str,
    as_of: date,
    book: ShadowBook,
    fills: list[ShadowPlan],
    new_plans: list[ShadowPlan],
    nav: dict[str, float],
) -> None:
    if not _configured():
        raise RuntimeError("Supabase 未配置，影子账本无法落库")
    account_id = assert_shadow_account(account_id)
    seed_shadow_account(account_id)
    # 先落计划状态再改账本：plan status 是成交幂等闸。若先写 cash/positions
    # 再 upsert plans 失败，下次 load_planned 仍会捞到旧 planned，买单被再兑一次。
    _upsert_plans(account_id, fills + new_plans)
    _upsert_account(account_id, as_of, book, nav)
    _replace_positions(account_id, book)
    _insert_events(account_id, as_of, fills)
    _upsert_nav(account_id, as_of, nav)


def _upsert_account(account_id: str, as_of: date, book: ShadowBook, nav: dict[str, float]) -> None:
    _table(TABLE_SHADOW_ACCOUNT).upsert(
        {
            "account_id": account_id,
            "cash": nav["cash"],
            "equity": nav["equity"],
            "market_value": nav["market_value"],
            "initial_capital": book.initial_capital,
            "as_of": as_of.isoformat(),
        },
        on_conflict="account_id",
    ).execute()


def _replace_positions(account_id: str, book: ShadowBook) -> None:
    _table(TABLE_SHADOW_POSITIONS).delete().eq("account_id", account_id).execute()
    rows = [_position_row(account_id, pos) for pos in book.positions.values() if pos.shares > 0]
    if rows:
        _table(TABLE_SHADOW_POSITIONS).insert(rows).execute()


def _upsert_plans(account_id: str, plans: list[ShadowPlan]) -> None:
    if not plans:
        return
    _table(TABLE_SHADOW_TRADE_PLANS).upsert(
        [_plan_row(account_id, plan) for plan in plans],
        on_conflict="plan_key",
    ).execute()


def _insert_events(account_id: str, as_of: date, fills: list[ShadowPlan]) -> None:
    from core.shadow_ledger import event_key

    rows = []
    for plan in fills:
        # event_type 按 schema 是 buy/sell，不是 filled/skipped。
        # 用 status 当 type/key 时，同日同股同量的买+卖会 upsert 互盖。
        rows.append(
            {
                "event_key": event_key(account_id, as_of, plan.action, plan.code, plan.qty, status=plan.status),
                "account_id": account_id,
                "as_of": as_of.isoformat(),
                "code": plan.code,
                "name": plan.name,
                "event_type": plan.action,
                "price": plan.entry_price,
                "qty": plan.qty,
                "fees": plan.fees,
                "reason": plan.fill_reason,
                "payload": {
                    "action": plan.action,
                    "plan_key": plan.plan_key,
                    "status": plan.status,
                },
            }
        )
    if rows:
        _table(TABLE_SHADOW_EVENTS).upsert(rows, on_conflict="event_key").execute()


def _upsert_nav(account_id: str, as_of: date, nav: dict[str, float]) -> None:
    _table(TABLE_SHADOW_NAV_DAILY).upsert(
        {"account_id": account_id, "as_of": as_of.isoformat(), **nav},
        on_conflict="account_id,as_of",
    ).execute()


def _row_to_position(row: dict[str, Any]) -> ShadowPosition:
    return ShadowPosition(
        code=str(row.get("code") or ""),
        name=str(row.get("name") or ""),
        shares=int(row.get("shares") or 0),
        sellable_shares=int(row.get("sellable_shares") or 0),
        avg_cost=float(row.get("avg_cost") or 0),
        buy_dt=_as_date(row.get("buy_dt")),
        last_mark=_opt_float(row.get("last_mark")),
        stop_loss=_opt_float(row.get("stop_loss")),
    )


def _row_to_plan(row: dict[str, Any]) -> ShadowPlan:
    return ShadowPlan(
        plan_key=str(row.get("plan_key") or ""),
        code=str(row.get("code") or ""),
        name=str(row.get("name") or ""),
        action=str(row.get("action") or ""),
        signal_date=_as_date(row.get("signal_date")) or date.today(),
        suggested_price=_opt_float(row.get("suggested_price")),
        stop_price=_opt_float(row.get("stop_price")),
        shares_hint=int(row.get("shares_hint") or 0),
        reason=str(row.get("reason") or ""),
        status=str(row.get("status") or "planned"),
        fill_reason=str(row.get("fill_reason") or ""),
        entry_date=_as_date(row.get("entry_date")),
        entry_price=_opt_float(row.get("entry_price")),
    )


def _position_row(account_id: str, pos: ShadowPosition) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "code": pos.code,
        "name": pos.name,
        "shares": pos.shares,
        "sellable_shares": pos.sellable_shares,
        "avg_cost": pos.avg_cost,
        "buy_dt": pos.buy_dt.isoformat() if pos.buy_dt else None,
        "last_mark": pos.last_mark,
        "stop_loss": pos.stop_loss,
    }


def _plan_row(account_id: str, plan: ShadowPlan) -> dict[str, Any]:
    return {
        "plan_key": plan.plan_key,
        "account_id": account_id,
        "code": plan.code,
        "name": plan.name,
        "action": plan.action,
        "status": plan.status,
        "signal_date": plan.signal_date.isoformat(),
        "entry_mode": "next_open",
        "suggested_price": plan.suggested_price,
        "stop_price": plan.stop_price,
        "shares_hint": plan.shares_hint,
        "reason": plan.reason,
        "entry_date": plan.entry_date.isoformat() if plan.entry_date else None,
        "entry_price": plan.entry_price,
        "fill_reason": plan.fill_reason,
    }


def _as_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _opt_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number
