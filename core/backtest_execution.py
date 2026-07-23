"""Backtest trade execution, price lookup, and NAV helpers."""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

CN_ZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_ENTRY_PRICE_TIME = "14:55"
logger = logging.getLogger(__name__)
IntradayPriceResult = tuple[float | None, str]
IntradayPriceFetcher = Callable[[str, date, str, dict], IntradayPriceResult]


@dataclass
class TradeRecord:
    signal_date: date
    entry_date: date | None
    exit_date: date
    code: str
    name: str
    trigger: str
    score: float
    entry_close: float
    exit_close: float
    ret_pct: float
    track: str = ""
    regime: str = ""
    entry_price_source: str = "daily_open"
    entry_target_time: str = ""
    exit_reason: str = "unknown"
    mfe_pct: float | None = None
    mae_pct: float | None = None
    signal_confirmed: bool = False
    entry_weight_multiplier: float = 1.0


@dataclass(frozen=True)
class _NavPosition:
    code: str
    entry_date: date
    exit_date: date
    entry_exec: float


@dataclass(frozen=True)
class ExitSimulationConfig:
    exit_mode: str
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    trailing_activate_pct: float
    sltp_priority: str
    atr_period: int
    atr_multiplier: float
    atr_hard_stop_pct: float


def calc_trade_excursion_pct(
    day_ohlc: dict[date, tuple[float, float, float, float]],
    window: list[date],
    entry_price: float,
) -> tuple[float | None, float | None]:
    if entry_price <= 0:
        return None, None
    max_high = entry_price
    min_low = entry_price
    for day in window:
        candle = day_ohlc.get(day)
        if candle is None:
            continue
        _, high, low, _ = candle
        max_high = max(max_high, float(high))
        min_low = min(min_low, float(low))
    return (max_high / entry_price - 1.0) * 100.0, (min_low / entry_price - 1.0) * 100.0


def close_on_date(df: pd.DataFrame, day: date) -> float | None:
    row = df[df["date"] == day]
    if row.empty:
        return None
    close = pd.to_numeric(row["close"], errors="coerce").dropna()
    return None if close.empty else float(close.iloc[-1])


def close_on_or_after(df: pd.DataFrame, day: date) -> tuple[float | None, date | None]:
    row = df[df["date"] >= day].head(1)
    if row.empty:
        return None, None
    close = pd.to_numeric(row["close"], errors="coerce").dropna()
    if close.empty:
        return None, None
    return float(close.iloc[0]), row.iloc[0]["date"]


def is_limit_up_locked(row_s: pd.Series) -> bool:
    try:
        open_px = float(row_s.get("open", 0))
        high = float(row_s.get("high", 0))
        low = float(row_s.get("low", 0))
        close = float(row_s.get("close", 0))
        if open_px <= 0:
            return False
        tolerance = open_px * 1e-6
        if abs(high - open_px) <= tolerance and abs(low - open_px) <= tolerance:
            return close >= open_px
    except (TypeError, ValueError):
        pass
    return False


def open_on_or_after(df: pd.DataFrame, day: date, *, skip_limit_up: bool = True) -> tuple[float | None, date | None]:
    candidates = df[df["date"] >= day].head(5)
    if candidates.empty:
        return None, None
    for _, row_s in candidates.iterrows():
        if skip_limit_up and is_limit_up_locked(row_s):
            continue
        if "open" in candidates.columns:
            open_px = pd.to_numeric(pd.Series([row_s["open"]]), errors="coerce").dropna()
            if not open_px.empty:
                return float(open_px.iloc[0]), row_s["date"]
        close = pd.to_numeric(pd.Series([row_s["close"]]), errors="coerce").dropna()
        if not close.empty:
            return float(close.iloc[0]), row_s["date"]
    return None, None


def parse_entry_time(raw: str) -> time:
    try:
        hour_s, minute_s = str(raw or DEFAULT_ENTRY_PRICE_TIME).strip().split(":", 1)
        return time(hour=int(hour_s), minute=int(minute_s))
    except (TypeError, ValueError):
        return time(hour=14, minute=55)


def intraday_ms_window(day: date, entry_time: str) -> tuple[int, int]:
    target = parse_entry_time(entry_time)
    start_dt = datetime.combine(day, time(hour=9, minute=30), tzinfo=CN_ZONE)
    end_dt = datetime.combine(day, target, tzinfo=CN_ZONE) + timedelta(minutes=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def price_at_or_before(df: pd.DataFrame, day: date, entry_time: str) -> float | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    work = df.copy()
    if "datetime" in work.columns:
        dt = pd.to_datetime(work["datetime"], errors="coerce")
    elif "timestamp" in work.columns:
        dt = pd.to_datetime(work["timestamp"], unit="ms", utc=True, errors="coerce").dt.tz_convert(CN_ZONE)
    else:
        return None
    work["datetime"] = dt
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    target = datetime.combine(day, parse_entry_time(entry_time), tzinfo=CN_ZONE)
    hit = work[(work["datetime"].dt.date == day) & (work["datetime"] <= target)].dropna(subset=["close"]).tail(1)
    return None if hit.empty else float(hit.iloc[0]["close"])


def resolve_intraday_entry_price(
    code: str,
    day: date,
    entry_time: str,
    cache: dict,
    price_fetcher: IntradayPriceFetcher | None,
) -> IntradayPriceResult:
    key = (str(code), day, str(entry_time))
    if key in cache:
        return cache[key]
    if price_fetcher is None:
        cache[key] = (None, "")
        return cache[key]
    try:
        cache[key] = price_fetcher(code, day, entry_time, cache)
    except Exception as exc:
        logger.warning("%s %s %s 分钟入场价失败，回退日线收盘: %s", code, day, entry_time, exc)
        cache[key] = (None, "")
    return cache[key]


def entry_on_or_after(
    df: pd.DataFrame,
    code: str,
    day: date,
    *,
    mode: str,
    entry_time: str,
    fallback: str,
    intraday_cache: dict,
    intraday_price_fetcher: IntradayPriceFetcher | None = None,
    skip_limit_up: bool = True,
) -> tuple[float | None, date | None, str]:
    candidates = df[df["date"] >= day].head(5)
    for _, row_s in candidates.iterrows():
        if skip_limit_up and is_limit_up_locked(row_s):
            continue
        hit_date = row_s["date"]
        if mode == "tail_1455":
            return _tail_entry_price(
                code, hit_date, row_s, entry_time, fallback, intraday_cache, intraday_price_fetcher
            )
        if mode == "close":
            price, entry_date = close_on_or_after(df, hit_date)
            return price, entry_date, "daily_close"
        price, entry_date = open_on_or_after(df, hit_date, skip_limit_up=False)
        return price, entry_date, "daily_open"
    return None, None, ""


def _tail_entry_price(
    code: str,
    hit_date: date,
    row_s: pd.Series,
    entry_time: str,
    fallback: str,
    intraday_cache: dict,
    intraday_price_fetcher: IntradayPriceFetcher | None,
) -> tuple[float | None, date | None, str]:
    price, source = resolve_intraday_entry_price(code, hit_date, entry_time, intraday_cache, intraday_price_fetcher)
    if price is not None and price > 0:
        return price, hit_date, source or f"intraday_1m_{entry_time}"
    if fallback == "error":
        raise RuntimeError(f"{code} {hit_date} {entry_time} 分钟线入场价缺失")
    if fallback == "skip":
        return None, None, "tail_1455_missing_skip"
    close = pd.to_numeric(pd.Series([row_s.get("close")]), errors="coerce").dropna()
    if not close.empty:
        return float(close.iloc[0]), hit_date, "daily_close_fallback"
    return None, None, ""


def close_on_or_before(
    df: pd.DataFrame,
    day: date,
    lower_exclusive: date | None = None,
) -> tuple[float | None, date | None]:
    row = df[df["date"] <= day]
    if lower_exclusive is not None:
        row = row[row["date"] > lower_exclusive]
    if row.empty:
        return None, None
    row = row.tail(1)
    close = pd.to_numeric(row["close"], errors="coerce").dropna()
    if close.empty:
        return None, None
    return float(close.iloc[0]), row.iloc[0]["date"]


def build_daily_ohlc_lookup(df: pd.DataFrame) -> dict[date, tuple[float, float, float, float]]:
    if df is None or df.empty:
        return {}
    cols = [c for c in ["date", "open", "high", "low", "close"] if c in df.columns]
    if "date" not in cols or "close" not in cols:
        return {}
    return _daily_ohlc_from_frame(df[cols].copy())


def _daily_ohlc_from_frame(work: pd.DataFrame) -> dict[date, tuple[float, float, float, float]]:
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.date
    for col in ["open", "high", "low", "close"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["date", "close"])
    return {row.date: _ohlc_tuple(row) for row in work.itertuples(index=False)}


def _ohlc_tuple(row) -> tuple[float, float, float, float]:
    close = float(row.close)
    open_px = float(row.open) if hasattr(row, "open") and pd.notna(row.open) else close
    high = float(row.high) if hasattr(row, "high") and pd.notna(row.high) else max(open_px, close)
    low = float(row.low) if hasattr(row, "low") and pd.notna(row.low) else min(open_px, close)
    return open_px, high, low, close


def ensure_ohlc_lookup_cache(
    records: list[TradeRecord],
    all_df_map: dict[str, pd.DataFrame],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
) -> None:
    for record in records:
        if record.code in ohlc_cache:
            continue
        df = all_df_map.get(record.code)
        if df is not None and not df.empty:
            ohlc_cache[record.code] = build_daily_ohlc_lookup(df)


def cash_mark_price_fn(
    all_df_map: dict[str, pd.DataFrame],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
):
    def _mark(code: str, day: date) -> float | None:
        if code not in ohlc_cache:
            df = all_df_map.get(code)
            if df is not None and not df.empty:
                ohlc_cache[code] = build_daily_ohlc_lookup(df)
        candle = ohlc_cache.get(code, {}).get(day)
        return float(candle[3]) if candle else None

    return _mark


def calc_atr_from_ohlc(
    sorted_dates: list[date],
    day_ohlc: dict[date, tuple[float, float, float, float]],
    as_of: date,
    period: int = 14,
) -> float | None:
    right = bisect.bisect_right(sorted_dates, as_of)
    if right < period + 1:
        return None
    window = sorted_dates[right - period - 1 : right]
    trs: list[float] = []
    for idx in range(1, len(window)):
        _, high, low, _ = day_ohlc[window[idx]]
        _, _, _, prev_close = day_ohlc[window[idx - 1]]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else None


def resolve_trade_exit(
    *,
    full_df: pd.DataFrame,
    day_ohlc: dict[date, tuple[float, float, float, float]],
    trade_dates: list[date],
    actual_entry_idx: int,
    actual_exit_idx: int,
    actual_exit_anchor: date,
    signal_date: date,
    entry_close: float,
    config: ExitSimulationConfig,
) -> tuple[float | None, date | None, str]:
    if config.exit_mode == "close_only":
        exit_close, exit_date = close_on_or_after(full_df, actual_exit_anchor)
        return exit_close, exit_date, "time_exit"
    market_window = trade_dates[actual_entry_idx + 1 : actual_exit_idx + 1]
    if config.exit_mode == "sltp":
        return _resolve_sltp_exit(
            full_df, day_ohlc, market_window, actual_exit_anchor, signal_date, entry_close, config
        )
    if config.exit_mode == "atr":
        sorted_dates = sorted(day_ohlc.keys())
        return _resolve_atr_exit(
            full_df, day_ohlc, sorted_dates, market_window, actual_exit_anchor, signal_date, entry_close, config
        )
    return None, None, "unknown"


def _resolve_sltp_exit(
    full_df: pd.DataFrame,
    day_ohlc: dict[date, tuple[float, float, float, float]],
    market_window: list[date],
    actual_exit_anchor: date,
    signal_date: date,
    entry_close: float,
    config: ExitSimulationConfig,
) -> tuple[float | None, date | None, str]:
    sl_price = entry_close * (1.0 + config.stop_loss_pct / 100.0) if config.stop_loss_pct < 0 else None
    tp_price = entry_close * (1.0 + config.take_profit_pct / 100.0) if config.take_profit_pct > 0 else None
    trailing_active = config.trailing_activate_pct <= 0
    activate_price = entry_close * (1.0 + config.trailing_activate_pct / 100.0) if not trailing_active else 0.0
    peak_high = entry_close
    prev_close = entry_close
    for market_day in market_window:
        candle = day_ohlc.get(market_day)
        if candle is None:
            continue
        open_px, high, low, close_px = candle
        if _is_limit_down_locked(open_px, high, low, prev_close):
            prev_close = close_px
            continue
        prev_close = close_px
        if config.trailing_stop_pct < 0 and not trailing_active and high >= activate_price:
            trailing_active = True
        trailing_price = _trailing_price(peak_high, trailing_active, config.trailing_stop_pct)
        hit = _sltp_exit_for_candle(open_px, high, low, sl_price, tp_price, trailing_price, config.sltp_priority)
        if hit is not None:
            exit_close, reason = hit
            return exit_close, market_day, reason
        peak_high = max(peak_high, high)
    return _time_exit(full_df, actual_exit_anchor, signal_date)


def _resolve_atr_exit(
    full_df: pd.DataFrame,
    day_ohlc: dict[date, tuple[float, float, float, float]],
    sorted_dates: list[date],
    market_window: list[date],
    actual_exit_anchor: date,
    signal_date: date,
    entry_close: float,
    config: ExitSimulationConfig,
) -> tuple[float | None, date | None, str]:
    atr_stop: float | None = None
    hard_floor = entry_close * (1.0 + config.atr_hard_stop_pct / 100.0)
    trailing_active = config.trailing_activate_pct <= 0
    activate_price = entry_close * (1.0 + config.trailing_activate_pct / 100.0) if not trailing_active else 0.0
    peak_high = entry_close
    prev_close = entry_close
    for market_day in market_window:
        candle = day_ohlc.get(market_day)
        if candle is None:
            continue
        open_px, high, low, close_px = candle
        if _is_limit_down_locked(open_px, high, low, prev_close):
            prev_close = close_px
            continue
        prev_close = close_px
        atr_stop = _updated_atr_stop(atr_stop, sorted_dates, day_ohlc, market_day, config)
        effective_stop = max(atr_stop or hard_floor, hard_floor)
        if config.trailing_stop_pct < 0 and not trailing_active and high >= activate_price:
            trailing_active = True
        trailing_price = _trailing_price(peak_high, trailing_active, config.trailing_stop_pct)
        hit = _atr_exit_for_candle(open_px, low, effective_stop, trailing_price)
        if hit is not None:
            exit_close, reason = hit
            return exit_close, market_day, reason
        peak_high = max(peak_high, high)
    return _time_exit(full_df, actual_exit_anchor, signal_date)


def _is_limit_down_locked(open_px: float, high: float, low: float, prev_close: float) -> bool:
    tolerance = open_px * 1e-6
    return open_px > 0 and abs(high - open_px) <= tolerance and abs(low - open_px) <= tolerance and open_px < prev_close


def _trailing_price(peak_high: float, trailing_active: bool, trailing_stop_pct: float) -> float | None:
    if trailing_stop_pct < 0 and trailing_active:
        return peak_high * (1.0 + trailing_stop_pct / 100.0)
    return None


def _sltp_exit_for_candle(
    open_px: float,
    high: float,
    low: float,
    sl_price: float | None,
    tp_price: float | None,
    trailing_price: float | None,
    priority: str,
) -> tuple[float, str] | None:
    checks = [("sl", sl_price), ("trail", trailing_price), ("tp", tp_price)]
    if priority != "stop_first":
        checks = [("tp", tp_price), ("trail", trailing_price), ("sl", sl_price)]
    for kind, price in checks:
        hit = _exit_hit(kind, price, open_px, high, low)
        if hit is not None:
            return hit
    return None


def _exit_hit(kind: str, price: float | None, open_px: float, high: float, low: float) -> tuple[float, str] | None:
    if price is None:
        return None
    if kind == "sl" and low <= price:
        return (price if open_px >= price else open_px), "stop_loss"
    if kind == "trail" and low <= price:
        return (price if open_px >= price else open_px), "trailing_stop"
    if kind == "tp" and high >= price:
        return (price if open_px <= price else open_px), "take_profit"
    return None


def _updated_atr_stop(
    atr_stop: float | None,
    sorted_dates: list[date],
    day_ohlc: dict[date, tuple[float, float, float, float]],
    market_day: date,
    config: ExitSimulationConfig,
) -> float | None:
    atr_value = calc_atr_from_ohlc(sorted_dates, day_ohlc, market_day, config.atr_period)
    if not atr_value or atr_value <= 0:
        return atr_stop
    close_px = day_ohlc[market_day][3]
    new_stop = close_px - config.atr_multiplier * atr_value
    return new_stop if atr_stop is None else max(atr_stop, new_stop)


def _atr_exit_for_candle(
    open_px: float,
    low: float,
    effective_stop: float,
    trailing_price: float | None,
) -> tuple[float, str] | None:
    if low <= effective_stop:
        return (effective_stop if open_px >= effective_stop else open_px), "atr_stop"
    if trailing_price is not None and low <= trailing_price:
        return (trailing_price if open_px >= trailing_price else open_px), "trailing_stop"
    return None


def _time_exit(
    full_df: pd.DataFrame, actual_exit_anchor: date, signal_date: date
) -> tuple[float | None, date | None, str]:
    exit_close, exit_date = close_on_or_before(full_df, actual_exit_anchor, lower_exclusive=signal_date)
    return exit_close, exit_date, "time_exit"


def build_daily_nav(
    records: list[TradeRecord],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
    trade_dates: list[date],
    start_dt: date,
    end_dt: date,
    buy_friction_pct: float = 0.0,
) -> pd.DataFrame:
    positions = _records_to_positions(records, trade_dates, buy_friction_pct)
    window = [day for day in trade_dates if start_dt <= day <= end_dt]
    if not positions or not window:
        return _empty_nav()
    cum_ret = 0.0
    prev_mtm: dict[int, float] = {}
    rows: list[dict] = []
    for day in window:
        daily_rets, open_count = _daily_position_returns(day, positions, ohlc_cache, prev_mtm)
        port_ret = sum(daily_rets) / open_count if open_count > 0 and daily_rets else 0.0
        cum_ret += port_ret
        rows.append(
            {"date": day, "nav": 1.0 + cum_ret, "daily_ret_pct": port_ret * 100.0, "positions_count": open_count}
        )
        _drop_closed_marks(day, positions, prev_mtm)
    return pd.DataFrame(rows)


def _empty_nav() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", "nav", "daily_ret_pct", "positions_count"])


def _records_to_positions(
    records: list[TradeRecord],
    trade_dates: list[date],
    buy_friction_pct: float,
) -> list[_NavPosition]:
    positions = []
    for record in records:
        entry_date = record.entry_date or _fallback_entry_date(record, trade_dates)
        entry_exec = record.entry_close * (1.0 + buy_friction_pct / 100.0)
        if entry_date is not None and entry_exec > 0:
            positions.append(_NavPosition(record.code, entry_date, record.exit_date, entry_exec))
    return positions


def _fallback_entry_date(record: TradeRecord, trade_dates: list[date]) -> date | None:
    try:
        signal_idx = next(idx for idx, day in enumerate(trade_dates) if day >= record.signal_date)
    except StopIteration:
        return None
    next_idx = signal_idx + 1
    return trade_dates[next_idx] if next_idx < len(trade_dates) else None


def _daily_position_returns(
    day: date,
    positions: list[_NavPosition],
    ohlc_cache: dict[str, dict[date, tuple[float, float, float, float]]],
    prev_mtm: dict[int, float],
) -> tuple[list[float], int]:
    daily_rets = []
    open_count = 0
    for idx, pos in enumerate(positions):
        if pos.entry_date > day or pos.exit_date < day:
            continue
        open_count += 1
        candle = ohlc_cache.get(pos.code, {}).get(day)
        if candle is None:
            daily_rets.append(0.0)
            continue
        close_today = candle[3]
        prev_price = prev_mtm.get(idx, pos.entry_exec)
        daily_rets.append(close_today / prev_price - 1.0 if prev_price > 0 else 0.0)
        prev_mtm[idx] = close_today
    return daily_rets, open_count


def _drop_closed_marks(day: date, positions: list[_NavPosition], prev_mtm: dict[int, float]) -> None:
    for idx in list(prev_mtm.keys()):
        if positions[idx].exit_date < day:
            del prev_mtm[idx]


def calc_portfolio_metrics(
    nav_df: pd.DataFrame,
    risk_free_annual: float = 2.0,
) -> dict:
    if nav_df is None or nav_df.empty or len(nav_df) < 2:
        return _empty_portfolio_metrics()
    nav = nav_df["nav"]
    daily_ret = nav_df["daily_ret_pct"] / 100.0
    n_days = len(nav_df)
    total_ret_pct = (float(nav.iloc[-1]) / float(nav.iloc[0]) - 1.0) * 100.0
    ann_ret_pct = total_ret_pct * (250.0 / max(n_days, 1))
    peak = nav.cummax()
    mdd_pct = float((nav / peak - 1.0).min()) * 100.0
    avg_pos = float(nav_df["positions_count"].mean()) if "positions_count" in nav_df.columns else 0.0
    return {
        "portfolio_sharpe": _portfolio_sharpe(daily_ret, risk_free_annual),
        "portfolio_mdd_pct": mdd_pct,
        "portfolio_calmar": ann_ret_pct / abs(mdd_pct) if mdd_pct < 0 else None,
        "portfolio_ann_ret_pct": ann_ret_pct,
        "portfolio_total_ret_pct": total_ret_pct,
        "portfolio_trading_days": n_days,
        "portfolio_avg_positions": avg_pos,
    }


def _empty_portfolio_metrics() -> dict:
    return {
        "portfolio_sharpe": None,
        "portfolio_mdd_pct": None,
        "portfolio_calmar": None,
        "portfolio_ann_ret_pct": None,
        "portfolio_total_ret_pct": None,
        "portfolio_trading_days": 0,
        "portfolio_avg_positions": 0.0,
    }


def _portfolio_sharpe(daily_ret: pd.Series, risk_free_annual: float) -> float | None:
    rf_daily = risk_free_annual / 100.0 / 250.0
    excess = daily_ret - rf_daily
    std_daily = float(excess.std(ddof=1))
    if std_daily > 0 and len(excess) >= 3:
        return float(excess.mean()) / std_daily * (250.0**0.5)
    return None
