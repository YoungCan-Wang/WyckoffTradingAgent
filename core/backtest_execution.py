"""Backtest trade execution, price lookup, and NAV helpers."""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from core.limit_move import limit_pct

CN_ZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_ENTRY_PRICE_TIME = "14:55"
LIMIT_TOUCH_TOLERANCE = 0.0015  # 相对涨跌停幅度的容差，吸收复权与挂单价四舍五入误差
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
    # 排序诊断用：score 是候选排序分（allocate_ai_candidates 输出，含阶段分/主升
    # +100/触发分等），alloc_score 与之相同、显式命名以免误读；watch_score 是
    # candidate_ranker 的 L3 质量分，在最终排序里只贡献 watch_score*8（上限 9.6）。
    # 两者必须分列记录，否则无法区分"排序主体无效"与"质量分无效"。
    alloc_score: float | None = None
    watch_score: float | None = None
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


def close_on_or_after(df: pd.DataFrame, day: date) -> tuple[float | None, date | None]:
    row = df[df["date"] >= day].head(1)
    if row.empty:
        return None, None
    close = pd.to_numeric(row["close"], errors="coerce").dropna()
    if close.empty:
        return None, None
    return float(close.iloc[0]), row.iloc[0]["date"]


def market_of_board(board: str) -> str:
    normalized = str(board or "").strip().lower()
    return normalized if normalized in {"us", "hk"} else "cn"


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) and number > 0 else None


def _with_prev_close(df: pd.DataFrame) -> pd.DataFrame:
    """补一列前收盘价，供涨跌停判定使用。

    前复权序列里同日价格与前收的比值等于原始比值（除权当日除外），所以涨跌停
    走涨跌幅比较，而不是 limit_move 的绝对涨跌停价——后者在复权序列上对不上。
    """
    work = df.reset_index(drop=True).copy()
    work["prev_close"] = pd.to_numeric(work["close"], errors="coerce").shift(1)
    return work


def _at_price_limit(row_s: pd.Series, code: str, *, price_field: str, market: str, upward: bool) -> bool:
    pct = limit_pct(code, market=market)
    if pct is None:
        return False
    prev_close = _positive_float(row_s.get("prev_close"))
    price = _positive_float(row_s.get(price_field))
    if prev_close is None or price is None:
        return False
    move_pct = (price / prev_close - 1.0) * 100.0
    threshold = pct * (1.0 - LIMIT_TOUCH_TOLERANCE)
    return move_pct >= threshold if upward else move_pct <= -threshold


def entry_blocked_by_limit_up(row_s: pd.Series, code: str, *, mode: str, market: str) -> bool:
    """该交易日能否按给定入场口径买到。

    开盘即封涨停的票在竞价阶段挂不进单，即便盘中被砸开（T 字板/烂板）也已经
    错过开盘价；收盘价口径则要看收盘是否封在涨停。
    """
    price_field = "open" if mode == "open" else "close"
    return _at_price_limit(row_s, code, price_field=price_field, market=market, upward=True)


def open_on_or_after(
    df: pd.DataFrame, day: date, code: str = "", *, skip_limit_up: bool = True, market: str = "cn"
) -> tuple[float | None, date | None]:
    candidates = _with_prev_close(df)
    candidates = candidates[candidates["date"] >= day].head(5)
    if candidates.empty:
        return None, None
    for _, row_s in candidates.iterrows():
        if skip_limit_up and entry_blocked_by_limit_up(row_s, code, mode="open", market=market):
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
    market: str = "cn",
) -> tuple[float | None, date | None, str]:
    candidates = _with_prev_close(df)
    candidates = candidates[candidates["date"] >= day].head(5)
    for _, row_s in candidates.iterrows():
        if skip_limit_up and entry_blocked_by_limit_up(row_s, code, mode=mode, market=market):
            continue
        hit_date = row_s["date"]
        if mode == "tail_1455":
            return _tail_entry_price(
                code, hit_date, row_s, entry_time, fallback, intraday_cache, intraday_price_fetcher
            )
        if mode == "close":
            price, entry_date = close_on_or_after(df, hit_date)
            return price, entry_date, "daily_close"
        price, entry_date = open_on_or_after(df, hit_date, code, skip_limit_up=False)
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
    code: str = "",
    market: str = "cn",
) -> tuple[float | None, date | None, str]:
    exit_ctx = _ExitContext(full_df, actual_exit_anchor, signal_date, code, market)
    if config.exit_mode == "close_only":
        exit_close, exit_date = _sellable_close_on_or_after(full_df, actual_exit_anchor, code, market)
        return exit_close, exit_date, "time_exit"
    market_window = trade_dates[actual_entry_idx + 1 : actual_exit_idx + 1]
    entry_date = trade_dates[actual_entry_idx] if 0 <= actual_entry_idx < len(trade_dates) else None
    locked_days = _limit_down_locked_days(day_ohlc, market_window, entry_date, code, market)
    if config.exit_mode == "sltp":
        return _resolve_sltp_exit(day_ohlc, market_window, locked_days, entry_close, config, exit_ctx)
    if config.exit_mode == "atr":
        return _resolve_atr_exit(day_ohlc, market_window, locked_days, entry_close, config, exit_ctx)
    return None, None, "unknown"


@dataclass(frozen=True)
class _ExitContext:
    full_df: pd.DataFrame
    actual_exit_anchor: date
    signal_date: date
    code: str
    market: str


def _limit_down_locked_days(
    day_ohlc: dict[date, tuple[float, float, float, float]],
    market_window: list[date],
    entry_date: date | None,
    code: str,
    market: str,
) -> set[date]:
    """按前一交易日收盘价识别一字跌停锁死日。

    入场价（尤其 open / tail_1455）不等于入场日收盘价，不能拿来当初收，否则
    入场日收阳后再封跌停会被漏判，止损会在不可成交日成交。
    """
    locked: set[date] = set()
    entry_candle = day_ohlc.get(entry_date) if entry_date is not None else None
    prev_close = entry_candle[3] if entry_candle is not None else None
    for market_day in market_window:
        candle = day_ohlc.get(market_day)
        if candle is None:
            continue
        open_px, high, low, close_px = candle
        if prev_close is not None and _is_limit_down_locked(open_px, high, low, prev_close, code, market):
            locked.add(market_day)
        prev_close = close_px
    return locked


def _sellable_close_on_or_after(
    full_df: pd.DataFrame, day: date, code: str, market: str
) -> tuple[float | None, date | None]:
    """一字跌停当天挂不出卖单，平仓顺延到之后第一个能成交的交易日。"""
    candidates = _with_prev_close(full_df)
    candidates = candidates[candidates["date"] >= day]
    for _, row_s in candidates.iterrows():
        if _row_limit_down_locked(row_s, code, market):
            continue
        close = _positive_float(row_s.get("close"))
        if close is not None:
            return close, row_s["date"]
    return None, None


def _row_limit_down_locked(row_s: pd.Series, code: str, market: str) -> bool:
    values = [_positive_float(row_s.get(field)) for field in ("open", "high", "low", "prev_close")]
    if any(value is None for value in values):
        return False
    open_px, high, low, prev_close = values
    return _is_limit_down_locked(open_px, high, low, prev_close, code, market)  # type: ignore[arg-type]


def _resolve_sltp_exit(
    day_ohlc: dict[date, tuple[float, float, float, float]],
    market_window: list[date],
    locked_days: set[date],
    entry_close: float,
    config: ExitSimulationConfig,
    ctx: _ExitContext,
) -> tuple[float | None, date | None, str]:
    sl_price = entry_close * (1.0 + config.stop_loss_pct / 100.0) if config.stop_loss_pct < 0 else None
    tp_price = entry_close * (1.0 + config.take_profit_pct / 100.0) if config.take_profit_pct > 0 else None
    trailing_active = config.trailing_activate_pct <= 0
    activate_price = entry_close * (1.0 + config.trailing_activate_pct / 100.0) if not trailing_active else 0.0
    peak_high = entry_close
    for market_day in market_window:
        candle = day_ohlc.get(market_day)
        if candle is None or market_day in locked_days:
            continue
        open_px, high, low, _close_px = candle
        if config.trailing_stop_pct < 0 and not trailing_active and high >= activate_price:
            trailing_active = True
        trailing_price = _trailing_price(peak_high, trailing_active, config.trailing_stop_pct)
        hit = _sltp_exit_for_candle(open_px, high, low, sl_price, tp_price, trailing_price, config.sltp_priority)
        if hit is not None:
            exit_close, reason = hit
            return exit_close, market_day, reason
        peak_high = max(peak_high, high)
    return _time_exit(ctx)


def _resolve_atr_exit(
    day_ohlc: dict[date, tuple[float, float, float, float]],
    market_window: list[date],
    locked_days: set[date],
    entry_close: float,
    config: ExitSimulationConfig,
    ctx: _ExitContext,
) -> tuple[float | None, date | None, str]:
    sorted_dates = sorted(day_ohlc.keys())
    atr_stop: float | None = None
    hard_floor = entry_close * (1.0 + config.atr_hard_stop_pct / 100.0)
    trailing_active = config.trailing_activate_pct <= 0
    activate_price = entry_close * (1.0 + config.trailing_activate_pct / 100.0) if not trailing_active else 0.0
    peak_high = entry_close
    for market_day in market_window:
        candle = day_ohlc.get(market_day)
        if candle is None or market_day in locked_days:
            continue
        open_px, high, low, _close_px = candle
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
    return _time_exit(ctx)


def _is_limit_down_locked(
    open_px: float, high: float, low: float, prev_close: float, code: str = "", market: str = "cn"
) -> bool:
    """一字跌停：全天封死在跌停价，挂单卖不出去。

    只看"全天无波动且下跌"会把窄幅横盘日也算进来，所以额外要求跌幅确实触及
    该板块的跌停幅度；拿不到板块幅度（港美股）时退回纯几何判据。
    """
    if open_px <= 0 or prev_close <= 0:
        return False
    tolerance = open_px * 1e-6
    if abs(high - open_px) > tolerance or abs(low - open_px) > tolerance or open_px >= prev_close:
        return False
    pct = limit_pct(code, market=market)
    if pct is None:
        return True
    drop_pct = (1.0 - open_px / prev_close) * 100.0
    return drop_pct >= pct * (1.0 - LIMIT_TOUCH_TOLERANCE)


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


def _time_exit(ctx: _ExitContext) -> tuple[float | None, date | None, str]:
    exit_close, exit_date = close_on_or_before(ctx.full_df, ctx.actual_exit_anchor, lower_exclusive=ctx.signal_date)
    if exit_date is None:
        return exit_close, exit_date, "time_exit"
    sellable_close, sellable_date = _sellable_close_on_or_after(ctx.full_df, exit_date, ctx.code, ctx.market)
    # 样本内始终锁死则无法成交，不能回退到跌停日收盘冒充成交。
    if sellable_date is None:
        return None, None, "time_exit"
    reason = "time_exit" if sellable_date == exit_date else "time_exit_limit_down_delayed"
    return sellable_close, sellable_date, reason


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
