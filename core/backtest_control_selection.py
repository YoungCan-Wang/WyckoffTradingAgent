"""Control selectors for the backtest replay: relative strength and seeded random.

These are deliberately naive stock-selection sources that share nothing with the Wyckoff
funnel except the universe snapshot, the liquidity floor and the execution path. They exist
so the funnel's confirmed signals can be judged against known-cheap alternatives under the
same regime gate, exits, friction and benchmark-excess protocol (#567, #576):

- ``rs``: 60-day return minus the benchmark's, descending.
- ``reversal``: the same score ascending (the mirror of ``rs``). Short-horizon reversal is
  the best-documented A-share cross-sectional effect, and ``rs`` landed below ``random``
  on #579, so the weakest side of the same ranking is the cheapest next thing to test.
- ``sector_rs``: stocks from the three sectors whose median ``rs`` score is highest, ordered
  by sector score and then by a seeded draw inside the sector, so sector momentum is tested
  without re-importing stock momentum (#599).
- ``random``: a uniform draw from the eligible universe. Its excess is the drag of the
  execution path itself (T+1 open entry, asymmetric stop/target, forced exit, friction),
  which is what any selection source must beat before it can be called alpha.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date
from statistics import median

import pandas as pd

from core.wyckoff_engine import FunnelConfig, dollar_volume_series

CONTROL_STRATEGIES = ("rs", "random", "reversal", "sector_rs")
RANDOM_TRIGGER = "RANDOM"
SECTOR_TOP_K = 3
SECTOR_MIN_SYMBOLS = 5
_TRIGGER_PREFIX = {"rs": "RS", "reversal": "REV", "sector_rs": "SECTOR"}


def control_trigger_name(strategy: str, window: int) -> str:
    return RANDOM_TRIGGER if strategy == "random" else f"{_TRIGGER_PREFIX[strategy]}{int(window)}"


def rank_relative_strength(
    day_df_map: dict[str, pd.DataFrame],
    bench_slice: pd.DataFrame,
    signal_date: date,
    cfg: FunnelConfig,
    *,
    window: int,
    weakest: bool = False,
) -> list[tuple[str, float]]:
    """Every eligible code by ``window``-day return minus the benchmark's, descending.

    ``weakest`` flips the order so the laggards come first (the ``reversal`` control).
    Truncation to ``top_n`` happens in the replay after the re-entry cooldown is applied.
    """
    bench_ret = _window_return_pct(bench_slice, window)
    if bench_ret is None:
        return []
    scored: list[tuple[str, float]] = []
    for code, df in eligible_history(day_df_map, signal_date, cfg).items():
        stock_ret = _window_return_pct(df, window)
        if stock_ret is not None:
            scored.append((code, stock_ret - bench_ret))
    scored.sort(key=lambda item: (item[1] if weakest else -item[1], item[0]))
    return scored


def rank_sector_strength(
    day_df_map: dict[str, pd.DataFrame],
    bench_slice: pd.DataFrame,
    signal_date: date,
    cfg: FunnelConfig,
    sector_map: dict[str, str],
    *,
    window: int,
    seed: int,
) -> list[tuple[str, float]]:
    """Eligible codes from the ``SECTOR_TOP_K`` sectors with the highest median excess return.

    The score is the sector's median so a sector is not carried by one runaway stock; sectors
    with fewer than ``SECTOR_MIN_SYMBOLS`` eligible names are skipped. Within a sector the order
    is a seeded draw keyed on the day, mirroring ``rank_random``.
    """
    by_sector: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, score in rank_relative_strength(day_df_map, bench_slice, signal_date, cfg, window=window):
        sector = sector_map.get(code)
        if sector:
            by_sector[sector].append((code, score))
    sector_scores = sorted(
        (
            (median(score for _, score in items), sector)
            for sector, items in by_sector.items()
            if len(items) >= SECTOR_MIN_SYMBOLS
        ),
        key=lambda item: (-item[0], item[1]),
    )[:SECTOR_TOP_K]
    rng = random.Random(f"{seed}:{signal_date.isoformat()}")
    ranked: list[tuple[str, float]] = []
    for sector_score, sector in sector_scores:
        codes = sorted(code for code, _ in by_sector[sector])
        rng.shuffle(codes)
        ranked.extend((code, sector_score) for code in codes)
    return ranked


def rank_random(
    day_df_map: dict[str, pd.DataFrame],
    signal_date: date,
    cfg: FunnelConfig,
    *,
    seed: int,
) -> list[tuple[str, float]]:
    """Eligible codes in a seeded uniform order; the score is the draw so reruns are identical.

    The generator is keyed on ``(seed, signal_date)`` rather than advanced across days, so a
    shard that starts on a different date still draws the same order for the same day.
    """
    rng = random.Random(f"{seed}:{signal_date.isoformat()}")
    scored = [(code, rng.random()) for code in sorted(eligible_history(day_df_map, signal_date, cfg))]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def eligible_history(
    day_df_map: dict[str, pd.DataFrame], signal_date: date, cfg: FunnelConfig
) -> dict[str, pd.DataFrame]:
    """Symbols traded on ``signal_date`` that clear the funnel's L1 liquidity and price floors."""
    return {
        code: df
        for code, df in day_df_map.items()
        if not df.empty and df["date"].iloc[-1] == signal_date and _liquidity_ok(df, cfg)
    }


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
