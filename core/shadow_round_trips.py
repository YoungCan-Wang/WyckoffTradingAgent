"""影子账本已清仓回合：从成交流水配对买卖，并量持有区间的最高/最低价。

纯计算，不读环境、不写库。一个回合 = 某只票从空仓建仓到再次回到空仓；
买价/卖价是该回合各笔 ``filled`` 成交价按股数加权（不含费用），即影子账本
次日开盘的真实成交价，不是信号日收盘价。仍持有的仓位不构成回合。

区间高低用不复权日线（与成交价同一口径），买入日与卖出日两端都计入。
影子账本在开盘成交：买入日整根日线都发生在建仓之后；卖出日只有开盘价在
持有期内，其高低包含卖出之后的盘中走势。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

ROUND_TRIP_PAYLOAD_KEY = "round_trip"
_HIGH_COLUMNS = ("最高", "high")
_LOW_COLUMNS = ("最低", "low")
_DATE_COLUMNS = ("日期", "date")


@dataclass(frozen=True)
class ShadowRoundTrip:
    code: str
    name: str
    qty: int
    buy_date: date
    buy_price: float
    sell_date: date
    sell_price: float
    sell_event_key: str


@dataclass(frozen=True)
class IntervalRange:
    low: float
    low_date: date
    high: float
    high_date: date


@dataclass
class _OpenLeg:
    name: str
    buy_date: date
    shares: int = 0
    bought_qty: int = 0
    bought_value: float = 0.0
    sold_qty: int = 0
    sold_value: float = 0.0


def closed_round_trips(events: list[dict[str, Any]]) -> list[ShadowRoundTrip]:
    """按代码把 filled 买卖配成回合；只返回已回到空仓的回合，按卖出日排序。"""
    legs: dict[str, _OpenLeg] = {}
    trips: list[ShadowRoundTrip] = []
    for event in sorted(_filled_trades(events), key=_trade_order):
        code, action, qty, price, day = event["code"], event["action"], event["qty"], event["price"], event["day"]
        leg = legs.get(code)
        if action == "buy":
            if leg is None:
                leg = legs[code] = _OpenLeg(name=event["name"], buy_date=day)
            leg.shares += qty
            leg.bought_qty += qty
            leg.bought_value += qty * price
            continue
        if leg is None or leg.shares <= 0:
            continue
        sold = min(qty, leg.shares)
        leg.shares -= sold
        leg.sold_qty += sold
        leg.sold_value += sold * price
        if leg.shares == 0:
            trips.append(_close_leg(code, leg, day, event["event_key"]))
            del legs[code]
    return sorted(trips, key=lambda trip: (trip.sell_date, trip.code))


def interval_high_low(bars: pd.DataFrame | None, start: date, end: date) -> IntervalRange | None:
    """``[start, end]`` 两端都含的日线最高/最低；缺列、缺数据或区间为空时返回 None。"""
    if bars is None or bars.empty:
        return None
    day_col, high_col, low_col = (_pick(bars, cols) for cols in (_DATE_COLUMNS, _HIGH_COLUMNS, _LOW_COLUMNS))
    if not (day_col and high_col and low_col):
        return None
    work = pd.DataFrame(
        {
            "day": pd.to_datetime(bars[day_col], errors="coerce").dt.date,
            "high": pd.to_numeric(bars[high_col], errors="coerce"),
            "low": pd.to_numeric(bars[low_col], errors="coerce"),
        }
    )
    work = work[(work["day"] >= start) & (work["day"] <= end)]
    highs, lows = work[work["high"] > 0], work[work["low"] > 0]
    if highs.empty or lows.empty:
        return None
    high_row = highs.loc[highs["high"].idxmax()]
    low_row = lows.loc[lows["low"].idxmin()]
    return IntervalRange(
        low=float(low_row["low"]),
        low_date=low_row["day"],
        high=float(high_row["high"]),
        high_date=high_row["day"],
    )


def round_trip_payload(trip: ShadowRoundTrip, interval: IntervalRange | None, source: str) -> dict[str, Any]:
    """写进清仓那笔 sell 事件 ``payload.round_trip`` 的内容；区间缺失时高低为 None，下次再补。"""
    return {
        "code": trip.code,
        "name": trip.name,
        "qty": trip.qty,
        "buy_date": trip.buy_date.isoformat(),
        "buy_price": round(trip.buy_price, 4),
        "sell_date": trip.sell_date.isoformat(),
        "sell_price": round(trip.sell_price, 4),
        "interval_low": round(interval.low, 4) if interval else None,
        "interval_low_date": interval.low_date.isoformat() if interval else None,
        "interval_high": round(interval.high, 4) if interval else None,
        "interval_high_date": interval.high_date.isoformat() if interval else None,
        "interval_basis": "daily_unadjusted_inclusive",
        "bar_source": source if interval else "",
    }


def needs_round_trip_payload(payload: object) -> bool:
    current = payload.get(ROUND_TRIP_PAYLOAD_KEY) if isinstance(payload, dict) else None
    if not isinstance(current, dict):
        return True
    return current.get("interval_high") is None or current.get("interval_low") is None


def _close_leg(code: str, leg: _OpenLeg, sell_date: date, event_key: str) -> ShadowRoundTrip:
    return ShadowRoundTrip(
        code=code,
        name=leg.name,
        qty=leg.bought_qty,
        buy_date=leg.buy_date,
        buy_price=leg.bought_value / leg.bought_qty,
        sell_date=sell_date,
        sell_price=leg.sold_value / leg.sold_qty,
        sell_event_key=event_key,
    )


def _filled_trades(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in events:
        action = str(row.get("event_type") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if action not in {"buy", "sell"} or (payload.get("status") or "filled") != "filled":
            continue
        qty, price, day = _int(row.get("qty")), _float(row.get("price")), _day(row.get("as_of"))
        code = str(row.get("code") or "").strip()
        if not code or qty <= 0 or price is None or day is None:
            continue
        out.append(
            {
                "code": code,
                "name": str(row.get("name") or ""),
                "action": action,
                "qty": qty,
                "price": price,
                "day": day,
                "event_key": str(row.get("event_key") or ""),
            }
        )
    return out


def _trade_order(event: dict[str, Any]) -> tuple[date, int, str]:
    # 同日先卖后买：与 run_shadow_session 的兑现顺序一致，清仓后同日再建仓是新回合。
    return event["day"], 0 if event["action"] == "sell" else 1, event["code"]


def _pick(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    return next((name for name in names if name in df.columns), None)


def _int(value: object) -> int:
    number = _float(value)
    return int(number) if number is not None else 0


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None
