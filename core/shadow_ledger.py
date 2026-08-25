"""Wyckoff 影子账本：盘后定计划、次日开盘价成交的纯计算。

与 ``USER_LIVE`` 实盘账本隔离；也不同于 ``ic_shadow`` / 动态影子分。
不读环境、不写库。成交日 T 只能兑现 signal_date < T 的计划，禁止用当日信号当日成交。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import pandas as pd

from core.backtest_execution import _at_price_limit, _with_prev_close, entry_blocked_by_limit_up
from core.cash_portfolio import CashPortfolioConfig, _shares_for_budget, calc_trade_cost
from core.concept_filters import is_user_facing_etf
from core.portfolio_symbol import portfolio_lot_size
from core.trade_fill import BUY, SELL, Fill, Holding, apply_fill

SHADOW_ACCOUNT_ID = "USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928"
INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS = 4
DEFAULT_STOP_PCT = 0.08


@dataclass
class ShadowPosition:
    code: str
    name: str = ""
    shares: int = 0
    sellable_shares: int = 0
    avg_cost: float = 0.0
    buy_dt: date | None = None
    last_mark: float | None = None
    stop_loss: float | None = None


@dataclass
class ShadowPlan:
    plan_key: str
    code: str
    name: str
    action: str
    signal_date: date
    suggested_price: float | None = None
    stop_price: float | None = None
    shares_hint: int = 0
    reason: str = ""
    status: str = "planned"
    fill_reason: str = ""
    entry_date: date | None = None
    entry_price: float | None = None
    qty: int = 0
    fees: dict[str, float] = field(default_factory=dict)


@dataclass
class ShadowBook:
    cash: float = INITIAL_CAPITAL
    initial_capital: float = INITIAL_CAPITAL
    positions: dict[str, ShadowPosition] = field(default_factory=dict)


@dataclass
class ShadowSession:
    book: ShadowBook
    fills: list[ShadowPlan]
    new_plans: list[ShadowPlan]
    nav: dict[str, float]


def plan_key(account_id: str, signal_date: date, action: str, code: str) -> str:
    return f"{account_id}:{signal_date.isoformat()}:{action}:{code}"


def event_key(account_id: str, as_of: date, event_type: str, code: str, qty: int) -> str:
    return f"{account_id}:{as_of.isoformat()}:{event_type}:{code}:{qty}"


def unlock_t_plus_one(book: ShadowBook, as_of: date) -> None:
    for pos in book.positions.values():
        if pos.buy_dt is not None and pos.buy_dt < as_of:
            pos.sellable_shares = pos.shares


def mark_to_market(book: ShadowBook, bars: dict[str, pd.DataFrame], as_of: date) -> None:
    for pos in book.positions.values():
        close = _bar_price(bars.get(pos.code), as_of, "close")
        if close is not None:
            pos.last_mark = close


def book_nav(book: ShadowBook) -> dict[str, float]:
    market_value = 0.0
    for pos in book.positions.values():
        mark = pos.last_mark if pos.last_mark and pos.last_mark > 0 else pos.avg_cost
        market_value += pos.shares * float(mark)
    equity = book.cash + market_value
    return {
        "cash": round(book.cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "pnl_total": round(equity - book.initial_capital, 2),
    }


def run_shadow_session(
    book: ShadowBook,
    planned: list[ShadowPlan],
    buy_candidates: list[dict[str, Any]],
    bars: dict[str, pd.DataFrame],
    as_of: date,
    *,
    account_id: str = SHADOW_ACCOUNT_ID,
    allow_new_buys: bool = True,
    prev_equity: float | None = None,
) -> ShadowSession:
    unlock_t_plus_one(book, as_of)
    fills = [filled for plan in planned if (filled := try_fill_plan(book, plan, bars, as_of)) is not None]
    mark_to_market(book, bars, as_of)
    sells = _stop_sell_plans(book, as_of, account_id)
    buys = propose_buy_plans(book, buy_candidates, bars, as_of, account_id) if allow_new_buys else []
    nav = book_nav(book)
    nav["pnl_day"] = round(nav["equity"] - float(prev_equity if prev_equity is not None else book.initial_capital), 2)
    return ShadowSession(book, fills, sells + buys, nav)


def try_fill_plan(
    book: ShadowBook,
    plan: ShadowPlan,
    bars: dict[str, pd.DataFrame],
    as_of: date,
) -> ShadowPlan | None:
    if plan.status != "planned":
        return None
    if plan.signal_date >= as_of:
        return replace(plan, status="skipped", fill_reason="lookahead_blocked")
    reason = _fill_block_reason(book, plan, bars, as_of)
    if reason:
        return replace(plan, status="skipped", fill_reason=reason, entry_date=as_of)
    price = _bar_price(bars.get(plan.code), as_of, "open")
    qty = _fill_qty(book, plan, price)
    fill = apply_fill(
        _holding_of(book, plan.code), book.cash, Fill(plan.code, plan.action, qty, price, as_of.isoformat(), plan.name)
    )
    _apply_fill_result(book, plan, fill, as_of, price, qty)
    return replace(
        plan,
        status="filled",
        fill_reason=f"next_open:{price:.4f}",
        entry_date=as_of,
        entry_price=price,
        qty=qty,
        fees={"fee": round(fill.fee, 2)},
        stop_price=plan.stop_price,
    )


def propose_buy_plans(
    book: ShadowBook,
    candidates: list[dict[str, Any]],
    bars: dict[str, pd.DataFrame],
    as_of: date,
    account_id: str,
) -> list[ShadowPlan]:
    open_codes = {code for code, pos in book.positions.items() if pos.shares > 0}
    slots = max(MAX_POSITIONS - len(open_codes), 0)
    if slots <= 0:
        return []
    nav = book_nav(book)
    budget = nav["equity"] / MAX_POSITIONS
    plans: list[ShadowPlan] = []
    for item in candidates:
        if slots <= 0:
            break
        plan = _candidate_buy_plan(book, item, bars, as_of, account_id, open_codes, budget)
        if plan is None:
            continue
        plans.append(plan)
        open_codes.add(plan.code)
        slots -= 1
    return plans


def _candidate_buy_plan(
    book: ShadowBook,
    item: dict[str, Any],
    bars: dict[str, pd.DataFrame],
    as_of: date,
    account_id: str,
    open_codes: set[str],
    budget: float,
) -> ShadowPlan | None:
    code = str(item.get("code") or "").strip()
    name = str(item.get("name") or code)
    if not code or code in open_codes or is_user_facing_etf(code, name):
        return None
    close = _bar_price(bars.get(code), as_of, "close")
    if close is None or close <= 0:
        return None
    shares = _shares_for_budget(close, book.cash, budget, CashPortfolioConfig())
    if shares < portfolio_lot_size(code):
        return None
    stop = _stop_price(item, close)
    return ShadowPlan(
        plan_key=plan_key(account_id, as_of, BUY, code),
        code=code,
        name=name,
        action=BUY,
        signal_date=as_of,
        suggested_price=close,
        stop_price=stop,
        shares_hint=shares,
        reason=str(item.get("tag") or item.get("recommend_reason") or "wyckoff_confirmed"),
    )


def _stop_sell_plans(book: ShadowBook, as_of: date, account_id: str) -> list[ShadowPlan]:
    plans: list[ShadowPlan] = []
    for pos in book.positions.values():
        if pos.shares <= 0 or pos.stop_loss is None or pos.last_mark is None:
            continue
        if pos.last_mark > pos.stop_loss:
            continue
        qty = pos.shares if pos.buy_dt is not None and pos.buy_dt < as_of else 0
        if qty <= 0:
            continue
        plans.append(
            ShadowPlan(
                plan_key=plan_key(account_id, as_of, SELL, pos.code),
                code=pos.code,
                name=pos.name,
                action=SELL,
                signal_date=as_of,
                suggested_price=pos.last_mark,
                stop_price=pos.stop_loss,
                shares_hint=qty,
                reason=f"stop:{pos.stop_loss:.4f}",
            )
        )
    return plans


def _fill_block_reason(book: ShadowBook, plan: ShadowPlan, bars: dict[str, pd.DataFrame], as_of: date) -> str:
    df = bars.get(plan.code)
    row = _bar_row(df, as_of)
    if row is None:
        return "no_open"
    price = _positive(row.get("open"))
    if price is None:
        return "no_open"
    if plan.action == BUY and entry_blocked_by_limit_up(row, plan.code, mode="open", market="cn"):
        return "limit_up"
    if plan.action == SELL and _at_price_limit(row, plan.code, price_field="open", market="cn", upward=False):
        return "limit_down"
    qty = _fill_qty(book, plan, price)
    lot = portfolio_lot_size(plan.code)
    if qty < lot or qty % lot:
        return "not_lot"
    if plan.action == SELL:
        pos = book.positions.get(plan.code)
        if pos is None or pos.sellable_shares < qty:
            return "t_plus_1"
        return ""
    gross = qty * price
    if book.cash + 1e-6 < gross + calc_trade_cost(gross, CashPortfolioConfig(), side="buy"):
        return "no_cash"
    return ""


def _fill_qty(book: ShadowBook, plan: ShadowPlan, price: float) -> int:
    lot = portfolio_lot_size(plan.code)
    if plan.action == SELL:
        pos = book.positions.get(plan.code)
        have = pos.sellable_shares if pos else 0
        qty = min(int(plan.shares_hint or have), have)
        return qty - (qty % lot)
    budget = book_nav(book)["equity"] / MAX_POSITIONS
    return _shares_for_budget(price, book.cash, min(budget, book.cash), CashPortfolioConfig())


def _apply_fill_result(book: ShadowBook, plan: ShadowPlan, fill, as_of: date, price: float, qty: int) -> None:
    book.cash = fill.cash
    if plan.action == BUY:
        holding = fill.holding
        book.positions[plan.code] = ShadowPosition(
            code=plan.code,
            name=plan.name or (holding.name if holding else ""),
            shares=holding.shares if holding else qty,
            sellable_shares=0,
            avg_cost=holding.cost_price if holding else price,
            buy_dt=as_of,
            last_mark=price,
            stop_loss=plan.stop_price,
        )
        return
    if fill.holding is None:
        book.positions.pop(plan.code, None)
        return
    pos = book.positions[plan.code]
    pos.shares = fill.holding.shares
    pos.sellable_shares = max(pos.sellable_shares - qty, 0)
    pos.name = fill.holding.name or pos.name


def _holding_of(book: ShadowBook, code: str) -> Holding | None:
    pos = book.positions.get(code)
    if pos is None or pos.shares <= 0:
        return None
    return Holding(code, pos.name, pos.shares, pos.avg_cost, pos.buy_dt.isoformat() if pos.buy_dt else "")


def _stop_price(item: dict[str, Any], close: float) -> float:
    for key in ("stop_loss", "stop_price", "support"):
        value = _positive(item.get(key))
        if value is not None:
            return value
    return round(close * (1.0 - DEFAULT_STOP_PCT), 4)


def _bar_row(df: pd.DataFrame | None, as_of: date) -> pd.Series | None:
    if df is None or df.empty or "date" not in df.columns:
        return None
    work = _with_prev_close(df)
    work["_day"] = pd.to_datetime(work["date"], errors="coerce").dt.date
    hit = work[work["_day"] == as_of]
    return None if hit.empty else hit.iloc[0]


def _bar_price(df: pd.DataFrame | None, as_of: date, field: str) -> float | None:
    row = _bar_row(df, as_of)
    return None if row is None else _positive(row.get(field))


def _positive(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
