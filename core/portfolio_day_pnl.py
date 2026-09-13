"""持仓与账本的当日盈亏：相对昨收，今日新开仓相对成交价。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.buy_dt import parse_buy_dt
from core.portfolio_symbol import normalize_portfolio_code
from core.portfolio_valuation import portfolio_currency

BASIS_PREV_CLOSE = "prev_close"
BASIS_TODAY_FILL = "today_fill"


@dataclass(frozen=True)
class PositionDayPnl:
    code: str
    name: str
    shares: int
    mark: float
    basis: float
    basis_kind: str
    day_pnl: float
    day_pnl_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "shares": self.shares,
            "mark": self.mark,
            "basis": self.basis,
            "basis_kind": self.basis_kind,
            "day_pnl": self.day_pnl,
            "day_pnl_pct": self.day_pnl_pct,
        }


@dataclass(frozen=True)
class BookDayPnl:
    day_pnl: float
    day_pnl_pct: float
    positions: tuple[PositionDayPnl, ...]
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


def opened_on_trade_date(buy_dt: Any, trade_date: str) -> bool:
    parsed = parse_buy_dt(str(buy_dt or "").strip())
    return parsed is not None and parsed.date().isoformat() == str(trade_date).strip()


def position_day_basis(row: dict[str, Any], trade_date: str, prev_close: float) -> tuple[float, str] | None:
    if opened_on_trade_date(row.get("buy_dt"), trade_date):
        cost = float(row.get("cost", row.get("cost_price", 0.0)) or 0.0)
        return (cost, BASIS_TODAY_FILL) if cost > 0 else None
    if prev_close > 0:
        return prev_close, BASIS_PREV_CLOSE
    return None


def calculate_book_day_pnl(
    free_cash: float,
    positions: list[dict[str, Any]],
    prices: dict[str, float],
    prev_closes: dict[str, float],
    cny_rates: dict[str, float],
    trade_date: str,
) -> BookDayPnl:
    """现金当日盈亏为 0；账本当日盈亏 = 各持仓当日盈亏之和。缺价则 complete=False。"""
    rows: list[PositionDayPnl] = []
    missing: list[str] = []
    day_pnl = 0.0
    sod_value = 0.0
    for row in positions:
        built = _one_position_day_pnl(row, prices, prev_closes, cny_rates, trade_date)
        if built is None:
            code = normalize_portfolio_code(str(row.get("code", "") or ""))
            if code and int(row.get("shares", 0) or 0) > 0:
                missing.append(code)
            continue
        item, sod_mark = built
        rows.append(item)
        day_pnl += item.day_pnl
        sod_value += sod_mark
    day_pnl = round(day_pnl, 2)
    sod_equity = sod_value + float(free_cash or 0.0)
    pct = round(day_pnl / sod_equity * 100.0, 4) if sod_equity > 0 else 0.0
    return BookDayPnl(day_pnl, pct, tuple(rows), tuple(sorted(set(missing))))


def book_day_pnl_from_stored(
    day_pnl: float,
    day_pnl_pct: float,
    positions: list[dict[str, Any]] | None,
) -> BookDayPnl:
    items = []
    for raw in positions or []:
        item = _stored_position(raw)
        if item is not None:
            items.append(item)
    return BookDayPnl(round(float(day_pnl), 2), round(float(day_pnl_pct), 4), tuple(items))


def _one_position_day_pnl(
    row: dict[str, Any],
    prices: dict[str, float],
    prev_closes: dict[str, float],
    cny_rates: dict[str, float],
    trade_date: str,
) -> tuple[PositionDayPnl, float] | None:
    code = normalize_portfolio_code(str(row.get("code", "") or ""))
    shares = int(row.get("shares", 0) or 0)
    if not code or shares <= 0:
        return None
    mark = float(prices.get(code, 0.0) or 0.0)
    rate = float(cny_rates.get(portfolio_currency(code), 0.0) or 0.0)
    basis_pair = position_day_basis(row, trade_date, float(prev_closes.get(code, 0.0) or 0.0))
    if mark <= 0 or rate <= 0 or basis_pair is None:
        return None
    basis, kind = basis_pair
    pnl = round(shares * (mark - basis) * rate, 2)
    pct = round((mark / basis - 1.0) * 100.0, 4)
    item = PositionDayPnl(
        code=code,
        name=str(row.get("name", "") or "").strip(),
        shares=shares,
        mark=mark,
        basis=basis,
        basis_kind=kind,
        day_pnl=pnl,
        day_pnl_pct=pct,
    )
    return item, shares * basis * rate


def _stored_position(raw: dict[str, Any]) -> PositionDayPnl | None:
    code = normalize_portfolio_code(str(raw.get("code", "") or ""))
    shares = int(raw.get("shares", 0) or 0)
    mark = float(raw.get("mark", 0.0) or 0.0)
    basis = float(raw.get("basis", 0.0) or 0.0)
    if not code or shares <= 0 or mark <= 0 or basis <= 0:
        return None
    kind = str(raw.get("basis_kind", "") or BASIS_PREV_CLOSE)
    if kind not in {BASIS_PREV_CLOSE, BASIS_TODAY_FILL}:
        kind = BASIS_PREV_CLOSE
    return PositionDayPnl(
        code=code,
        name=str(raw.get("name", "") or "").strip(),
        shares=shares,
        mark=mark,
        basis=basis,
        basis_kind=kind,
        day_pnl=round(float(raw.get("day_pnl", 0.0) or 0.0), 2),
        day_pnl_pct=round(float(raw.get("day_pnl_pct", 0.0) or 0.0), 4),
    )
