"""Relative-strength (RS) baseline selector for the backtest replay.

This is a deliberately naive stock-selection source that shares nothing with the Wyckoff
funnel except the universe snapshot, the liquidity floor and the execution path. It exists
so the funnel's confirmed signals can be judged against a known-cheap alternative under the
same regime gate, exits, friction and benchmark-excess protocol (#567).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.wyckoff_engine import FunnelConfig, dollar_volume_series

RS_TRACK = "rs"


def rs_trigger_name(window: int) -> str:
    return f"RS{int(window)}"


def rank_relative_strength(
    day_df_map: dict[str, pd.DataFrame],
    bench_slice: pd.DataFrame,
    signal_date: date,
    cfg: FunnelConfig,
    *,
    window: int,
) -> list[tuple[str, float]]:
    """Every eligible code by ``window``-day return minus the benchmark's, descending.

    Symbols are dropped when their history is stale on ``signal_date`` (suspended), shorter
    than the window, below the funnel's L1 liquidity / price floors, or not positively priced.
    Truncation to ``top_n`` happens in the replay after the re-entry cooldown is applied.
    """
    bench_ret = _window_return_pct(bench_slice, window)
    if bench_ret is None:
        return []
    scored: list[tuple[str, float]] = []
    for code, df in day_df_map.items():
        if df.empty or df["date"].iloc[-1] != signal_date or not _liquidity_ok(df, cfg):
            continue
        stock_ret = _window_return_pct(df, window)
        if stock_ret is not None:
            scored.append((code, stock_ret - bench_ret))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _window_return_pct(df: pd.DataFrame, window: int) -> float | None:
    close = pd.to_numeric(df.get("close"), errors="coerce").dropna()
    if len(close) <= window:
        return None
    start, end = float(close.iloc[-1 - window]), float(close.iloc[-1])
    if start <= 0 or end <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _liquidity_ok(df: pd.DataFrame, cfg: FunnelConfig) -> bool:
    close = pd.to_numeric(df.get("close"), errors="coerce").dropna()
    if close.empty or float(close.iloc[-1]) < float(cfg.l1_min_close_price):
        return False
    amount = dollar_volume_series(df).tail(max(int(cfg.amount_avg_window), 1))
    return amount.empty or float(amount.mean()) >= float(cfg.min_avg_amount_wan) * 10000
