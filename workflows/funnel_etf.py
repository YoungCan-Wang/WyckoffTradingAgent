"""ETF funnel enhancement workflow."""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from core.funnel_etf import build_etf_funnel_config, rank_etf_candidates
from core.wyckoff_engine import FunnelConfig, layer1_filter, layer2_strength_detailed
from integrations import funnel_etf_data
from integrations.tickflow_notice import TICKFLOW_UPGRADE_URL
from tools.data_fetcher import fetch_all_ohlcv
from tools.ohlcv_fallback_fetcher import FetchRuntimeConfig
from workflows.fetch_runtime_config import fetch_runtime_config_from_env

logger = logging.getLogger(__name__)


def run_etf_enhancement(
    base_cfg: FunnelConfig,
    window,
    bench_df: pd.DataFrame | None,
    *,
    direct_source: bool = False,
) -> tuple[list[str], dict[str, str], dict[str, pd.DataFrame], list[str], list[dict]]:
    etf_symbols, etf_sector_map = funnel_etf_data.load_etf_universe()
    etf_df_map = fetch_etf_ohlcv(
        etf_symbols,
        window,
        direct_source=direct_source,
        runtime_config=fetch_runtime_config_from_env(),
    )
    etf_l2_passed, etf_candidates = rank_fetched_etfs(base_cfg, bench_df, etf_df_map, etf_sector_map)
    _log_etf_summary(etf_df_map, etf_l2_passed)
    return etf_symbols, etf_sector_map, etf_df_map, etf_l2_passed, etf_candidates


def fetch_etf_ohlcv(
    etf_symbols: list[str],
    window: Any,
    *,
    batch_size: int = 50,
    direct_source: bool = False,
    runtime_config: FetchRuntimeConfig | None = None,
) -> dict[str, pd.DataFrame]:
    if not etf_symbols:
        return {}
    if not _has_market_data_source():
        logger.warning("[funnel] ETF 板块增强需要数据源，跳过。购买 TickFlow：%s", TICKFLOW_UPGRADE_URL)
        return {}
    df_map, _ = fetch_all_ohlcv(
        symbols=etf_symbols,
        window=window,
        batch_size=batch_size,
        max_workers=4,
        batch_timeout=120,
        batch_sleep=1,
        executor_mode="thread",
        direct_source=direct_source,
        runtime_config=runtime_config,
    )
    _fill_missing_funds(df_map, etf_symbols, window)
    if not df_map:
        logger.warning("[funnel] ETF 行情拉取失败，跳过板块增强")
    return df_map


def _fill_missing_funds(df_map: dict[str, pd.DataFrame], symbols: list[str], window: Any) -> None:
    from integrations.tushare_fund_data import fetch_fund_daily

    for symbol in symbols:
        if symbol in df_map:
            continue
        try:
            frame = fetch_fund_daily(symbol, window.start_trade_date, window.end_trade_date)
        except Exception as exc:
            logger.debug("fund fallback failed for %s: %s", symbol, exc)
            continue
        if frame is not None and not frame.empty:
            df_map[symbol] = frame


def _has_market_data_source() -> bool:
    return bool(os.getenv("TICKFLOW_API_KEY", "").strip()) or bool(os.getenv("TUSHARE_TOKEN", "").strip())


def rank_fetched_etfs(
    base_cfg: FunnelConfig,
    bench_df: pd.DataFrame | None,
    etf_df_map: dict[str, pd.DataFrame],
    etf_sector_map: dict[str, str],
) -> tuple[list[str], list[dict]]:
    if not etf_df_map:
        return [], []
    etf_cfg = build_etf_funnel_config(base_cfg)
    etf_l1 = layer1_filter(list(etf_df_map.keys()), {}, {}, etf_df_map, etf_cfg)
    if not etf_l1:
        return [], []
    etf_l2, etf_channel_map, _ = layer2_strength_detailed(etf_l1, etf_df_map, bench_df, etf_cfg, rps_universe=etf_l1)
    return etf_l2, rank_etf_candidates(etf_l2, etf_df_map, etf_sector_map, etf_channel_map)


def _log_etf_summary(etf_df_map: dict[str, pd.DataFrame], etf_l2_passed: list[str]) -> None:
    if etf_df_map:
        logger.info("[funnel] ETF板块增强: fetched=%s, L2=%s", len(etf_df_map), len(etf_l2_passed))
    else:
        logger.info("[funnel] ETF板块增强: 跳过")
