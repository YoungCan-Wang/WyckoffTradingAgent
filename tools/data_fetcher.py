"""OHLCV fetch facade.

This module keeps the public batch-fetch API stable while concrete fetching
strategies live in dedicated modules.
"""

from __future__ import annotations

import logging

import pandas as pd

import tools.ohlcv_fallback_fetcher as ohlcv_fallback_fetcher
import tools.tickflow_batch_fetcher as tickflow_batch_fetcher
from core.hist_dates import latest_trade_date_from_hist as latest_trade_date_from_hist

logger = logging.getLogger(__name__)


def fetch_all_ohlcv(
    symbols: list[str],
    window,
    *,
    enforce_target_trade_date: bool = False,
    batch_size: int = ohlcv_fallback_fetcher.BATCH_SIZE,
    max_workers: int = ohlcv_fallback_fetcher.MAX_WORKERS,
    batch_timeout: int = ohlcv_fallback_fetcher.BATCH_TIMEOUT,
    batch_sleep: float = ohlcv_fallback_fetcher.BATCH_SLEEP,
    executor_mode: str = ohlcv_fallback_fetcher.EXECUTOR_MODE,
    direct_source: bool = False,
    runtime_config: ohlcv_fallback_fetcher.FetchRuntimeConfig | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, int | float]]:
    """Fetch daily OHLCV using the fastest available strategy."""
    batch_result = tickflow_batch_fetcher.fetch_tickflow_daily_batch(
        symbols=symbols,
        window=window,
        enforce_target_trade_date=enforce_target_trade_date,
        batch_size=batch_size,
        batch_sleep=batch_sleep,
    )
    if batch_result is not None:
        return _complete_partial_batch(
            batch_result,
            symbols,
            window,
            enforce_target_trade_date=enforce_target_trade_date,
            batch_size=batch_size,
            max_workers=max_workers,
            batch_timeout=batch_timeout,
            batch_sleep=batch_sleep,
            executor_mode=executor_mode,
            direct_source=direct_source,
            runtime_config=runtime_config,
        )

    return ohlcv_fallback_fetcher.fetch_ohlcv_fallback(
        symbols=symbols,
        window=window,
        enforce_target_trade_date=enforce_target_trade_date,
        batch_size=batch_size,
        max_workers=max_workers,
        batch_timeout=batch_timeout,
        batch_sleep=batch_sleep,
        executor_mode=executor_mode,
        direct_source=direct_source,
        runtime_config=runtime_config,
    )


def _complete_partial_batch(
    batch_result,
    symbols: list[str],
    window,
    **fallback_kwargs,
) -> tuple[dict[str, pd.DataFrame], dict[str, int | float | list[str]]]:
    df_map, raw_stats = batch_result
    stats = dict(raw_stats)
    missing = [symbol for symbol in symbols if symbol not in df_map]
    if not missing:
        return df_map, stats
    suspended = _suspended_symbols(window.end_trade_date)
    retry_symbols = [symbol for symbol in missing if symbol not in suspended]
    if retry_symbols:
        fallback_map, fallback_stats = ohlcv_fallback_fetcher.fetch_ohlcv_fallback(
            symbols=retry_symbols,
            window=window,
            **fallback_kwargs,
        )
        df_map.update(fallback_map)
        stats["partial_fallback_requested"] = len(retry_symbols)
        stats["partial_fallback_ok"] = len(fallback_map)
        stats["partial_fallback_fail"] = int(fallback_stats.get("fetch_fail", 0) or 0)
    stats["suspended_symbols"] = sorted(symbol for symbol in missing if symbol in suspended)
    stats["raw_fetch_missing"] = max(len(symbols) - len(df_map), 0)
    stats["excluded_non_trading"] = len(stats["suspended_symbols"])
    stats["fetch_ok"] = len(df_map)
    stats["fetch_fail"] = len([symbol for symbol in symbols if symbol not in df_map and symbol not in suspended])
    return df_map, stats


def _suspended_symbols(trade_date) -> set[str]:
    try:
        from integrations.market_metadata import fetch_suspended_symbols

        return fetch_suspended_symbols(trade_date)
    except Exception as exc:
        logger.warning("suspension lookup failed; retrying all missing symbols: %s", exc)
        return set()
