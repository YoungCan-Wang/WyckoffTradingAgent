"""Fixed-horizon event labels for recommendation tracking."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import datetime, time
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from core.backtest_execution import entry_blocked_by_limit_up
from core.market_trade_cost import market_of
from core.trade_friction import round_trip_cost_pct
from utils.safe import safe_float


def build_horizon_event(
    row: dict[str, Any],
    ohlc: dict[str, dict[str, float]],
    *,
    horizon_days: int = 5,
    target_pct: float = 10.0,
) -> dict[str, Any]:
    horizon = max(int(horizon_days), 1)
    target = float(target_pct)
    trade_dates = sorted(ohlc)
    recommend_date = recommend_date_to_yyyymmdd(row.get("recommend_date"))
    window = _future_window(trade_dates, recommend_date, horizon + 1, ohlc)
    entry_date = window[0][0] if window else ""
    entry_price = safe_float(window[0][1].get("open")) if window else 0.0
    base = _base_event(row, recommend_date, entry_date, entry_price, horizon, target)
    status = _entry_status(row, ohlc.get(recommend_date), window)
    if status:
        return {**base, "label_ready": False, "label_status": status}
    code = str(row.get("code") or "")
    metrics = _window_metrics(window, entry_price, horizon, target, market_of(code).lower())
    cost = round_trip_cost_pct(code=code)
    close_return = (window[-1][1]["close"] / entry_price - 1.0) * 100.0
    return {
        **base,
        **metrics,
        "availability_verified": True,
        "round_trip_cost_pct": round(cost, 6),
        "net_close_return_horizon_pct": round(close_return - cost, 6),
    }


def summarize_horizon_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [event for event in events if event.get("label_ready")]
    hits = [event for event in ready if event.get("hit_target")]
    drawdowns = [safe_float(event.get("mae_horizon_pct")) for event in ready]
    mfes = [safe_float(event.get("mfe_horizon_pct")) for event in ready]
    close_returns = _present_numbers(ready, "close_return_horizon_pct")
    net_returns = _present_numbers(ready, "net_close_return_horizon_pct")
    winners = [value for value in close_returns if value > 0]
    losers = [value for value in close_returns if value < 0]
    avg_win = _avg(winners)
    avg_loss = _avg(losers)
    return {
        "rows_total": len(events),
        "rows_ready": len(ready),
        "rows_unready": len(events) - len(ready),
        "status_counts": dict(Counter(str(event.get("label_status") or "unknown") for event in events)),
        "net_return_rows": len(net_returns),
        "avg_net_close_return_horizon_pct": _avg(net_returns),
        "net_close_win_rate_pct": _pct(sum(value > 0 for value in net_returns), len(net_returns)),
        "hit_count": len(hits),
        "hit_rate_pct": _pct(len(hits), len(ready)),
        "close_win_count": len(winners),
        "close_win_rate_pct": _pct(len(winners), len(close_returns)),
        "avg_close_return_horizon_pct": _avg(close_returns),
        "avg_winning_close_return_pct": avg_win,
        "avg_losing_close_return_pct": avg_loss,
        "close_payoff_ratio": _ratio(avg_win, abs(avg_loss) if avg_loss is not None else None),
        "avg_mfe_horizon_pct": _avg(mfes),
        "avg_mae_horizon_pct": _avg(drawdowns),
        "mfe_mae_ratio": _ratio(_avg(mfes), abs(_avg(drawdowns)) if drawdowns else None),
        "mae_le_neg5_count": sum(value <= -5.0 for value in drawdowns),
        "mae_le_neg5_rate_pct": _pct(sum(value <= -5.0 for value in drawdowns), len(drawdowns)),
    }


def _base_event(
    row: dict[str, Any],
    recommend_date: str,
    entry_date: str,
    entry_price: float,
    horizon: int,
    target: float,
) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "code": row.get("code"),
        "name": row.get("name"),
        "recommend_date": int(recommend_date) if recommend_date.isdigit() else None,
        "entry_date": int(entry_date) if entry_date.isdigit() else None,
        "entry_price": round(entry_price, 4) if entry_price > 0 else 0.0,
        "entry_convention": "next_observed_open",
        "exit_convention": "entry_plus_h_observed_bars_close",
        "return_basis": "per_recommendation_event",
        "tracking_initial_price": _safe_optional_float(row.get("initial_price")),
        "execution_verified": False,
        "publication_time_present": bool(row.get("created_at")),
        "availability_verified": False,
        "horizon_days": horizon,
        "target_pct": target,
        "is_ai_recommended": bool(row.get("is_ai_recommended")),
        "funnel_score": _safe_optional_float(row.get("funnel_score")),
        "recommend_count": _safe_optional_int(row.get("recommend_count")),
    }


def _entry_status(
    row: dict[str, Any],
    signal_row: dict[str, float] | None,
    window: list[tuple[str, dict[str, float]]],
) -> str:
    if not signal_row or safe_float(signal_row.get("close")) <= 0:
        return "missing_signal_close"
    if not window or safe_float(window[0][1].get("open")) <= 0:
        return "missing_entry_open"
    entry_date, candle = window[0]
    if "volume" in candle and safe_float(candle["volume"]) <= 0:
        return "entry_suspended"
    code = str(row.get("code") or "")
    market = market_of(code).lower()
    if entry_blocked_by_limit_up(
        pd.Series({**candle, "prev_close": signal_row["close"]}), code, mode="open", market=market
    ):
        return "entry_limit_up"
    if not row.get("created_at"):
        return "missing_publication_time"
    zone = ZoneInfo("America/New_York" if market == "us" else "Asia/Shanghai")
    try:
        published = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if published.tzinfo is None:
            return "invalid_publication_time"
        opening = datetime.combine(datetime.strptime(entry_date, "%Y%m%d").date(), time(9, 30), zone)
        if published >= opening:
            return "published_after_entry"
    except ValueError:
        return "invalid_publication_time"
    if any(not _valid_price_bar(bar) for _, bar in window):
        return "invalid_price_window"
    return ""


def _valid_price_bar(bar: dict[str, float]) -> bool:
    high, low, close = (safe_float(bar.get(key)) for key in ("high", "low", "close"))
    if not 0 < low <= close <= high:
        return False
    return "open" not in bar or low <= safe_float(bar["open"]) <= high


def _future_window(
    trade_dates: list[str],
    entry_date: str,
    horizon: int,
    ohlc: dict[str, dict[str, float]],
) -> list[tuple[str, dict[str, float]]]:
    return [(day, ohlc[day]) for day in trade_dates if day > entry_date][:horizon]


def _window_metrics(
    window: list[tuple[str, dict[str, float]]],
    entry_price: float,
    horizon: int,
    target: float,
    market: str,
) -> dict[str, Any]:
    high_date, high_row = max(window, key=lambda item: item[1]["high"])
    low_date, low_row = min(window, key=lambda item: item[1]["low"])
    close_date, close_row = window[-1]
    mfe = (float(high_row["high"]) / entry_price - 1.0) * 100.0
    mae = (float(low_row["low"]) / entry_price - 1.0) * 100.0
    close_ret = (float(close_row["close"]) / entry_price - 1.0) * 100.0
    price_touch = _first_hit_date(window, entry_price, target)
    sellable = window[1:] if market == "cn" else window
    first_hit = _first_hit_date(sellable, entry_price, target)
    return {
        "label_ready": len(window) > horizon,
        "label_status": "ready" if len(window) > horizon else "partial_window",
        "observed_days": max(len(window) - 1, 0),
        "mfe_horizon_pct": round(mfe, 2),
        "mae_horizon_pct": round(mae, 2),
        "close_return_horizon_pct": round(close_ret, 2),
        "mfe_horizon_date": int(high_date),
        "mae_horizon_date": int(low_date),
        "window_end_date": int(close_date),
        "price_touch_target": bool(price_touch),
        "price_touch_first_date": int(price_touch) if price_touch else None,
        "hit_target": bool(first_hit),
        "target_hit_basis": "sellable_day_price_touch_not_fill",
        "first_hit_date": int(first_hit) if first_hit else None,
        "days_to_hit": _days_to_hit(window, first_hit),
    }


def _first_hit_date(window: list[tuple[str, dict[str, float]]], entry_price: float, target: float) -> str:
    for day, row in window:
        if (float(row["high"]) / entry_price - 1.0) * 100.0 >= target:
            return day
    return ""


def _days_to_hit(window: list[tuple[str, dict[str, float]]], first_hit: str) -> int | None:
    if not first_hit:
        return None
    for idx, (day, _) in enumerate(window):
        if day == first_hit:
            return idx
    return None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _present_numbers(events: list[dict[str, Any]], key: str) -> list[float]:
    return [value for event in events if (value := _safe_optional_float(event.get(key))) is not None]


def _pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100.0, 2) if denominator else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator, 2)


def _safe_optional_float(raw: Any) -> float | None:
    try:
        value = float(raw)
        return value if isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _safe_optional_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def recommend_date_to_yyyymmdd(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if len(text) == 8 and text.isdigit():
        return text
    try:
        return datetime.fromisoformat(text).strftime("%Y%m%d")
    except ValueError:
        return ""


def pick_close_on_or_before(sorted_trade_dates: list[str], target_yyyymmdd: str) -> str:
    if not sorted_trade_dates or not target_yyyymmdd:
        return ""
    index = bisect_right(sorted_trade_dates, target_yyyymmdd) - 1
    return "" if index < 0 else sorted_trade_dates[index]
