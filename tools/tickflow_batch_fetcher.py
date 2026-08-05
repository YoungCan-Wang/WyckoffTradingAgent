"""TickFlow daily OHLCV batch fetcher."""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd

from core.hist_dates import latest_trade_date_from_hist
from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.tickflow_client import TickFlowClient, normalize_cn_symbol
from tools.spot_patch import append_spot_bar_if_needed
from utils.trading_clock import CN_TZ

logger = logging.getLogger(__name__)

TICKFLOW_BATCH_ENABLED = os.getenv("FUNNEL_ENABLE_TICKFLOW_BATCH", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _tickflow_window_params(window) -> tuple[int, int, int]:
    start_d = window.start_trade_date
    end_d = window.end_trade_date
    start_dt = datetime.combine(start_d, datetime.min.time(), tzinfo=CN_TZ)
    end_dt = datetime.combine(end_d + timedelta(days=1), datetime.min.time(), tzinfo=CN_TZ) - timedelta(milliseconds=1)
    day_span = (end_d - start_d).days + 1
    count = min(max(day_span * 2 + 16, 64), 10000)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000), count


def _should_use_tickflow_batch() -> bool:
    return TICKFLOW_BATCH_ENABLED and bool(os.getenv("TICKFLOW_API_KEY", "").strip())


def _normalize_batch_df(df: pd.DataFrame, target_trade_date: date | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    out = normalize_hist_from_fetch(df)
    out.attrs.update(getattr(df, "attrs", {}) or {})
    out.attrs.setdefault("source", "tickflow_batch")
    if target_trade_date is None:
        return out
    trimmed = out[pd.to_datetime(out["date"], errors="coerce").dt.date <= target_trade_date].copy()
    return trimmed if not trimmed.empty else None


def _fetch_tickflow_daily_batch(
    client: TickFlowClient,
    batch: list[str],
    *,
    count: int,
    start_ms: int,
    end_ms: int,
    batch_no: int,
) -> dict[str, pd.DataFrame] | None:
    try:
        return client.get_klines_batch(
            batch,
            period="1d",
            count=count,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            adjust="forward",
        )
    except Exception as e:
        logger.warning("TickFlow daily batch #%s failed: %s", batch_no, e)
        return None


def _merge_tickflow_batch(
    df_map: dict[str, pd.DataFrame],
    fetched: dict[str, pd.DataFrame],
    raw_by_norm: dict[str, str],
    target_trade_date: date,
    enforce_target_trade_date: bool,
) -> int:
    patched_count = 0
    for norm_sym, raw_sym in raw_by_norm.items():
        df = _normalize_batch_df(fetched.get(norm_sym), target_trade_date)
        if df is None:
            continue
        if enforce_target_trade_date and latest_trade_date_from_hist(df) != target_trade_date:
            df, patched = append_spot_bar_if_needed(raw_sym, df, target_trade_date)
            patched_count += int(patched)
        if not enforce_target_trade_date or latest_trade_date_from_hist(df) == target_trade_date:
            df_map[raw_sym] = df
    return patched_count


def _tickflow_fetch_stats(
    symbol_count: int,
    df_map: dict[str, pd.DataFrame],
    fetch_spot_patched: int,
    started: float,
    failed_batches: int = 0,
) -> dict[str, int]:
    elapsed = time.monotonic() - started
    return {
        "fetch_ok": len(df_map),
        "fetch_fail": max(symbol_count - len(df_map), 0),
        "fetch_date_mismatch": 0,
        "fetch_spot_patched": fetch_spot_patched,
        "failed_batches": failed_batches,
        "fetch_elapsed_s": int(elapsed),
        "fetch_qps": int(len(df_map) / elapsed) if elapsed > 0 else 0,
    }


def fetch_tickflow_daily_batch(
    symbols: list[str],
    window,
    enforce_target_trade_date: bool,
    batch_size: int,
    batch_sleep: float,
) -> tuple[dict[str, pd.DataFrame], dict[str, int]] | None:
    if not _should_use_tickflow_batch():
        return None
    client = TickFlowClient(api_key=os.getenv("TICKFLOW_API_KEY", "").strip())
    start_ms, end_ms, count = _tickflow_window_params(window)
    total_batches = (len(symbols) + batch_size - 1) // batch_size if symbols else 0
    df_map: dict[str, pd.DataFrame] = {}
    fetch_spot_patched = 0
    failed_batches = 0
    started = time.monotonic()
    logger.info(
        "TickFlow daily batch start: symbols=%s, batches=%s, batch_size=%s", len(symbols), total_batches, batch_size
    )
    for index in range(0, len(symbols), batch_size):
        batch_no = index // batch_size + 1
        batch = symbols[index : index + batch_size]
        raw_by_norm = {normalize_cn_symbol(sym): sym for sym in batch}
        logger.info("TickFlow daily batch #%s/%s symbols=%s", batch_no, total_batches, len(batch))
        fetched = _fetch_tickflow_daily_batch(
            client, batch, count=count, start_ms=start_ms, end_ms=end_ms, batch_no=batch_no
        )
        if fetched is None:
            failed_batches += 1
            continue
        fetch_spot_patched += _merge_tickflow_batch(
            df_map, fetched, raw_by_norm, window.end_trade_date, enforce_target_trade_date
        )
        if index + batch_size < len(symbols) and batch_sleep > 0:
            time.sleep(batch_sleep)
    if not df_map:
        return None
    stats = _tickflow_fetch_stats(len(symbols), df_map, fetch_spot_patched, started, failed_batches)
    _log_tickflow_batch_result(symbols, df_map, failed_batches, stats)
    return df_map, stats


def _log_tickflow_batch_result(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    failed_batches: int,
    stats: dict[str, int],
) -> None:
    missing = max(len(symbols) - len(df_map), 0)
    if failed_batches or missing:
        missing_sample = ",".join([s for s in symbols if s not in df_map][:8])
        logger.warning(
            "TickFlow daily batch partial: ok=%s, missing=%s, failed_batches=%s, sample_missing=%s, elapsed=%ss",
            stats["fetch_ok"],
            missing,
            failed_batches,
            missing_sample or "-",
            stats["fetch_elapsed_s"],
        )
    else:
        logger.info(
            "TickFlow daily batch done: ok=%s, fail=%s, elapsed=%ss",
            stats["fetch_ok"],
            stats["fetch_fail"],
            stats["fetch_elapsed_s"],
        )
