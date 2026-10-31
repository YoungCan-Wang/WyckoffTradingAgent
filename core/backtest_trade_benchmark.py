"""Per-trade benchmark returns over each trade's own holding window.

Without them a cash-account gain in a rising market reads as alpha; the strategy
evidence rules require excess over the benchmark, net of cost, per trade.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

BENCHMARK_COLUMNS = ("bench_main_ret_pct", "bench_smallcap_ret_pct")


def attach_benchmark_returns(
    trades_df: pd.DataFrame,
    bench_df: pd.DataFrame | None,
    smallcap_bench_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if trades_df.empty:
        return trades_df
    for column, bench in zip(BENCHMARK_COLUMNS, (bench_df, smallcap_bench_df), strict=True):
        trades_df[column] = _window_returns(trades_df, _price_table(bench))
    return trades_df


def _price_table(bench: pd.DataFrame | None) -> dict[date, tuple[float, float]]:
    if bench is None or bench.empty or "close" not in bench.columns or "date" not in bench.columns:
        return {}
    opens = bench["open"] if "open" in bench.columns else bench["close"]
    table: dict[date, tuple[float, float]] = {}
    for day, open_, close in zip(bench["date"], opens, bench["close"], strict=True):
        close_value = _finite(close)
        if close_value is None:
            continue
        open_value = _finite(open_)
        table[day] = (open_value if open_value is not None else close_value, close_value)
    return table


def _window_returns(trades_df: pd.DataFrame, table: dict[date, tuple[float, float]]) -> list[float]:
    if not table:
        return [math.nan] * len(trades_df)
    sources = trades_df["entry_price_source"] if "entry_price_source" in trades_df.columns else [""] * len(trades_df)
    return [
        _window_return(table, entry, exit_, str(source or ""))
        for entry, exit_, source in zip(trades_df["entry_date"], trades_df["exit_date"], sources, strict=True)
    ]


def _window_return(table: dict[date, tuple[float, float]], entry: object, exit_: object, source: str) -> float:
    start, end = table.get(entry), table.get(exit_)  # type: ignore[arg-type]
    if start is None or end is None:
        return math.nan
    # 入场按 T+1 开盘成交时基准也取开盘，否则入场日的大盘涨跌会被算进超额。
    entry_price = start[0] if "open" in source else start[1]
    return (end[1] / entry_price - 1.0) * 100.0 if entry_price > 0 else math.nan


def _finite(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None
