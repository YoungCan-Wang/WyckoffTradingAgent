from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from core.backtest_rs_selection import rank_relative_strength
from core.wyckoff_engine import FunnelConfig

_START = date(2026, 1, 1)


def _hist(closes: list[float], *, amount: float = 1e9, last_date: date | None = None) -> pd.DataFrame:
    dates = [_START + timedelta(days=i) for i in range(len(closes))]
    if last_date is not None:
        dates[-1] = last_date
    return pd.DataFrame({"date": dates, "close": closes, "amount": [amount] * len(closes)})


def _cfg() -> FunnelConfig:
    cfg = FunnelConfig()
    cfg.min_avg_amount_wan = 4000.0
    cfg.amount_avg_window = 3
    cfg.l1_min_close_price = 2.0
    return cfg


def test_ranks_by_excess_return_over_benchmark_descending() -> None:
    signal_date = _START + timedelta(days=4)
    bench = _hist([100, 100, 100, 100, 110])
    day_df_map = {
        "weak": _hist([10, 10, 10, 10, 10.5]),
        "strong": _hist([10, 10, 10, 10, 13]),
        "mid": _hist([10, 10, 10, 10, 12]),
    }
    ranked = rank_relative_strength(day_df_map, bench, signal_date, _cfg(), window=2)
    assert [code for code, _ in ranked] == ["strong", "mid", "weak"]
    assert ranked[0][1] == pytest.approx(30.0 - 10.0)


def test_drops_stale_short_illiquid_and_cheap_symbols() -> None:
    signal_date = _START + timedelta(days=4)
    bench = _hist([100, 100, 100, 100, 100])
    day_df_map = {
        "stale": _hist([10, 10, 10, 10, 20], last_date=signal_date - timedelta(days=1)),
        "short": _hist([10, 20]),
        "illiquid": _hist([10, 10, 10, 10, 20], amount=1e6),
        "penny": _hist([1.0, 1.0, 1.0, 1.0, 1.5]),
        "ok": _hist([10, 10, 10, 10, 11]),
    }
    ranked = rank_relative_strength(day_df_map, bench, signal_date, _cfg(), window=2)
    assert [code for code, _ in ranked] == ["ok"]


def test_returns_empty_without_benchmark_window() -> None:
    signal_date = _START + timedelta(days=4)
    day_df_map = {"ok": _hist([10, 10, 10, 10, 11])}
    assert rank_relative_strength(day_df_map, _hist([100, 101]), signal_date, _cfg(), window=2) == []
