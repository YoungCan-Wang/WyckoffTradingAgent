"""Tests for as-of funnel replay (historical backfill)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from workflows.market_funnel_asof import build_asof_pool, truncate_history
from workflows.market_funnel_runtime import runtime_config_from_env


def _frame(start: str, rows: int, *, close: float = 10.0, volume: float = 1_000_000.0) -> pd.DataFrame:
    dates = pd.date_range(start, periods=rows, freq="B")
    return pd.DataFrame({"date": dates, "close": close, "high": close, "low": close, "volume": volume})


class TestTruncateHistory:
    def test_cuts_rows_after_as_of(self):
        out = truncate_history(_frame("2026-01-05", 10), date(2026, 1, 9))
        assert out is not None
        assert pd.to_datetime(out["date"]).dt.date.max() == date(2026, 1, 9)
        assert len(out) == 5

    def test_returns_none_when_as_of_absent(self):
        # 目标日无该标的数据（停牌/未上市）时不能用前一日冒充。
        assert truncate_history(_frame("2026-01-05", 10), date(2026, 1, 10)) is None

    def test_returns_none_for_empty(self):
        assert truncate_history(None, date(2026, 1, 9)) is None
        assert truncate_history(pd.DataFrame(), date(2026, 1, 9)) is None


class TestBuildAsofPool:
    def test_ranks_by_as_of_dollar_volume_not_latest(self, monkeypatch):
        monkeypatch.delenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", raising=False)
        monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "80")
        runtime = runtime_config_from_env("us", None)

        # BIG 在 as_of 当日成交额更大；SMALL 只在 as_of 之后放量，不应影响 as_of 排名。
        big = _frame("2026-01-01", 120, close=10.0, volume=5_000_000.0)
        small = _frame("2026-01-01", 120, close=10.0, volume=1_000_000.0)
        small.loc[small.index[-1], "volume"] = 99_000_000_000.0

        as_of = pd.to_datetime(small["date"]).dt.date.iloc[-5]
        pool = build_asof_pool({"BIG.US": big, "SMALL.US": small}, {}, runtime, as_of)

        assert [row["symbol"] for row in pool.ranked] == ["BIG.US", "SMALL.US"]
        for frame in pool.df_map.values():
            assert pd.to_datetime(frame["date"]).dt.date.max() == as_of

    def test_drops_symbols_without_as_of_bar(self, monkeypatch):
        monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "80")
        runtime = runtime_config_from_env("us", None)
        frame = _frame("2026-01-01", 120)
        as_of = pd.to_datetime(frame["date"]).dt.date.iloc[-1]
        halted = frame.iloc[:-1].copy()

        pool = build_asof_pool({"OK.US": frame, "HALT.US": halted}, {}, runtime, as_of)

        assert [row["symbol"] for row in pool.ranked] == ["OK.US"]
        assert "HALT.US" not in pool.df_map

    def test_applies_price_and_amount_floors(self, monkeypatch):
        monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "80")
        monkeypatch.setenv("MARKET_FUNNEL_MIN_QUOTE_PRICE", "1.0")
        monkeypatch.setenv("MARKET_FUNNEL_MIN_QUOTE_AMOUNT", "1000000")
        runtime = runtime_config_from_env("us", None)
        rich = _frame("2026-01-01", 120, close=10.0, volume=1_000_000.0)
        cheap = _frame("2026-01-01", 120, close=0.4, volume=1_000_000.0)
        thin = _frame("2026-01-01", 120, close=10.0, volume=10.0)
        as_of = pd.to_datetime(rich["date"]).dt.date.iloc[-1]

        pool = build_asof_pool({"RICH.US": rich, "CHEAP.US": cheap, "THIN.US": thin}, {}, runtime, as_of)

        assert [row["symbol"] for row in pool.ranked] == ["RICH.US"]

    def test_name_falls_back_to_symbol(self, monkeypatch):
        monkeypatch.setenv("MARKET_FUNNEL_MIN_HISTORY_ROWS", "80")
        runtime = runtime_config_from_env("us", None)
        frame = _frame("2026-01-01", 120)
        as_of = pd.to_datetime(frame["date"]).dt.date.iloc[-1]

        pool = build_asof_pool({"AAPL.US": frame}, {"AAPL.US": ""}, runtime, as_of)

        assert pool.ranked[0]["name"] == "AAPL.US"
