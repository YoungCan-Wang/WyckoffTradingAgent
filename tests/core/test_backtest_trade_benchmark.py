from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from core.backtest_trade_benchmark import attach_benchmark_returns


def _bench() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)],
            "open": [100.0, 102.0, 105.0],
            "close": [101.0, 104.0, 110.0],
        }
    )


def _trades(source: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entry_date": [date(2025, 1, 2), date(2025, 1, 3), None],
            "exit_date": [date(2025, 1, 6), date(2025, 1, 6), date(2025, 1, 6)],
            "entry_price_source": [source, source, source],
            "ret_pct": [5.0, 1.0, 0.0],
        }
    )


def test_attach_benchmark_returns_uses_open_for_open_entries_and_close_otherwise() -> None:
    by_open = attach_benchmark_returns(_trades("daily_open"), _bench(), None)
    assert by_open["bench_main_ret_pct"].tolist()[:2] == pytest.approx([10.0, (110.0 / 102.0 - 1) * 100])
    assert math.isnan(by_open["bench_main_ret_pct"].iloc[2])
    assert by_open["bench_smallcap_ret_pct"].isna().all()

    by_close = attach_benchmark_returns(_trades("daily_close"), _bench(), _bench())
    assert by_close["bench_main_ret_pct"].iloc[0] == pytest.approx((110.0 / 101.0 - 1) * 100)
    assert by_close["bench_smallcap_ret_pct"].iloc[1] == pytest.approx((110.0 / 104.0 - 1) * 100)


def test_attach_benchmark_returns_tolerates_missing_columns_and_empty_trades() -> None:
    empty = attach_benchmark_returns(pd.DataFrame(), _bench(), None)
    assert empty.empty and "bench_main_ret_pct" not in empty.columns

    calendar_only = pd.DataFrame({"date": [date(2025, 1, 2), date(2025, 1, 6)]})
    out = attach_benchmark_returns(_trades("daily_open"), calendar_only, None)
    assert out["bench_main_ret_pct"].isna().all()
