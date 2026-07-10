from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from workflows.backtest_snapshot_fetch_hk_us import _fetch_benchmark, _market_config, _save_snapshot


@pytest.mark.parametrize(
    "market,symbol,name",
    [("hk", "00700.HK", "Tencent"), ("us", "AAPL.US", "Apple")],
)
def test_save_snapshot_outputs_required_artifacts(market, symbol, name, tmp_path) -> None:
    config = _market_config(market)
    full_df = pd.DataFrame({"date": ["2026-01-02"], "symbol": [symbol], "close": [350.0]})
    bench_df = pd.DataFrame({"date": ["2026-01-02"], "close": [18000.0]})

    status = _save_snapshot(
        tmp_path,
        full_df,
        bench_df,
        "BENCH.TEST",
        [symbol],
        1,
        {symbol: name},
        date(2025, 1, 1),
        date(2026, 1, 2),
        config,
    )

    assert status == 0
    assert (tmp_path / "hist_full.csv.gz").exists()
    assert (tmp_path / "benchmark_main.csv").exists()
    assert json.loads((tmp_path / "name_map.json").read_text(encoding="utf-8")) == {symbol: name}
    meta = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert meta["market"] == market
    assert meta["benchmark"] == "BENCH.TEST"


@pytest.mark.parametrize("market", ["hk", "us"])
def test_save_snapshot_fails_without_benchmark(market, tmp_path) -> None:
    config = _market_config(market)
    full_df = pd.DataFrame({"date": ["2026-01-02"], "symbol": ["X"], "close": [350.0]})

    status = _save_snapshot(
        tmp_path, full_df, None, "", ["X"], 1, {"X": "Name"}, date(2025, 1, 1), date(2026, 1, 2), config
    )

    assert status == 1


class _FakeBenchmarkClient:
    """模拟第一个候选代码取不到数据、第二个候选生效的场景。"""

    def __init__(self, valid_symbol: str) -> None:
        self.valid_symbol = valid_symbol
        self.requested: list[str] = []

    def get_klines_batch(self, symbols, **_kwargs):
        symbol = symbols[0]
        self.requested.append(symbol)
        if symbol != self.valid_symbol:
            return {}
        return {symbol: pd.DataFrame({"date": pd.date_range("2026-01-01", periods=60), "close": range(60)})}


def test_hk_fetch_benchmark_falls_back_to_next_candidate() -> None:
    config = _market_config("hk")
    client = _FakeBenchmarkClient(valid_symbol="03033.HK")

    bench_df, bench_symbol = _fetch_benchmark(client, count=60, start_ms=0, end_ms=1, config=config)

    assert bench_symbol == "03033.HK"
    assert bench_df is not None and not bench_df.empty
    assert client.requested[0] != "03033.HK"
    assert "03033.HK" in client.requested


def test_hk_fetch_benchmark_returns_none_when_all_candidates_fail() -> None:
    config = _market_config("hk")
    client = _FakeBenchmarkClient(valid_symbol="NOT_IN_LIST")

    bench_df, bench_symbol = _fetch_benchmark(client, count=60, start_ms=0, end_ms=1, config=config)

    assert bench_df is None
    assert bench_symbol == ""
