"""Artifact and metric wiring for the cross-sectional factor backtest."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.cash_portfolio import CashPortfolioConfig
from core.factor_portfolio import FactorPortfolioConfig
from workflows.factor_backtest import (
    PANEL_COLUMNS,
    load_prepared_panel,
    run_sweep,
    summarise,
    write_artifacts,
    yearly_breakdown,
)


def test_factor_panel_cache_has_an_explicit_generation() -> None:
    workflow = Path(".github/workflows/factor_backtest.yml").read_text(encoding="utf-8")

    assert 'PANEL_CACHE_VERSION: "v2"' in workflow
    assert "key: factor-panel-${{ inputs.start }}-${{ inputs.end }}-${{ env.PANEL_CACHE_VERSION }}" in workflow


def _result(nav_values: list[float], trades: pd.DataFrame) -> dict:
    index = pd.bdate_range("2024-01-02", periods=len(nav_values))
    return {
        "nav": pd.Series(nav_values, index=index),
        "positions": pd.Series(10, index=index),
        "cash": pd.Series(100.0, index=index),
        "trades": trades,
    }


class TestSummarise:
    def test_costs_and_turnover_are_scaled_by_average_equity(self) -> None:
        """分母用初始资金会在净值翻倍/腰斩时把摩擦率算歪。"""
        trades = pd.DataFrame(
            {"shares": [100, 100], "price": [10.0, 10.0], "cost": [5.0, 5.0], "side": ["buy", "sell"]}
        )
        cfg = FactorPortfolioConfig(top_n=10, rebalance_days=5, costs=CashPortfolioConfig(initial_cash=1_000.0))
        flat = summarise(
            _result([1_000.0] * 244, trades), pd.Series(1.0, index=pd.bdate_range("2024-01-02", periods=244)), cfg
        )
        doubled = summarise(
            _result(list(np.linspace(1_000.0, 3_000.0, 244)), trades),
            pd.Series(1.0, index=pd.bdate_range("2024-01-02", periods=244)),
            cfg,
        )
        assert flat["cost_drag_annual_pct"] > doubled["cost_drag_annual_pct"]
        assert flat["annual_turnover_pct"] == pytest.approx(100.0, rel=0.05)

    def test_alpha_is_annualised_difference_against_the_benchmark(self) -> None:
        index = pd.bdate_range("2024-01-02", periods=244)
        nav = pd.Series(np.linspace(1_000.0, 1_200.0, 244), index=index)
        benchmark = pd.Series(np.linspace(1.0, 1.1, 244), index=index)
        cfg = FactorPortfolioConfig(top_n=10, rebalance_days=5, costs=CashPortfolioConfig(initial_cash=1_000.0))
        got = summarise({**_result(list(nav), pd.DataFrame()), "nav": nav}, benchmark, cfg)
        assert got["annual_return_pct"] == pytest.approx(20.0, abs=0.5)
        assert got["benchmark_annual_pct"] == pytest.approx(10.0, abs=0.5)
        assert got["annual_alpha_pct"] == pytest.approx(10.0, abs=0.5)


class TestYearlyBreakdown:
    def test_splits_by_calendar_year(self) -> None:
        index = pd.to_datetime(["2023-06-01", "2023-12-29", "2024-01-02", "2024-12-31"])
        nav = pd.Series([1.0, 1.2, 1.2, 0.9], index=index)
        benchmark = pd.Series([1.0, 1.1, 1.1, 1.1], index=index)
        out = yearly_breakdown(nav, benchmark)
        assert list(out["year"]) == [2023, 2024]
        assert out.loc[0, "alpha_pct"] == pytest.approx(10.0)
        assert out.loc[1, "return_pct"] == pytest.approx(-25.0)


def _fake_panel_dir(root):
    panel_dir = root / "panel"
    panel_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2024-01-02", periods=30)
    rows = []
    for symbol in (600000, 600001, 600002, 600003):
        for i, date in enumerate(dates):
            price = 10.0 + symbol % 7 + i * 0.1
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": price,
                    "close": price,
                    "pre_close": price,
                    "vol": 1_000.0,
                    "amount": 50_000.0,
                    "adj_factor": 1.0,
                    "pb": 1.0 + symbol % 5,
                    "pe_ttm": 10.0 + symbol % 5,
                    "ps_ttm": 2.0 + symbol % 5,
                    "dv_ttm": 1.0,
                    "circ_mv": 1e6,
                    "turnover_rate_f": 1.0,
                }
            )
    pd.DataFrame(rows)[PANEL_COLUMNS].to_parquet(panel_dir / "panel_2024.parquet", index=False)
    pd.DataFrame({"symbol": [600000, 600001, 600002, 600003], "list_date": ["20100101"] * 4}).to_parquet(
        panel_dir / "universe.parquet", index=False
    )
    return panel_dir


class TestArtifacts:
    def test_end_to_end_writes_matrix_nav_and_report(self, tmp_path) -> None:
        panel = load_prepared_panel(
            _fake_panel_dir(tmp_path),
            start="2024-01-02",
            end="2024-02-28",
            exclude_st=False,
            min_amount_thousand=0.0,
            min_listed_days=0,
        )
        cfg = FactorPortfolioConfig(top_n=2, rebalance_days=5, costs=CashPortfolioConfig(initial_cash=100_000.0))
        matrix, detail = run_sweep(panel, [cfg], progress=lambda _msg: None)
        out = write_artifacts(tmp_path / "out", matrix, detail, cfg)

        assert {"sweep_matrix.csv", "nav.csv", "trades.csv", "yearly.csv", "summary.json", "summary.md"} <= {
            p.name for p in out.iterdir()
        }
        payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert payload["best"] == "top2_reb5_buf2"
        assert payload["config"]["costs"]["initial_cash"] == 100_000.0
        nav = pd.read_csv(out / "nav.csv")
        assert {"nav", "benchmark", "daily_equal_weight"} <= set(nav.columns)
        assert "截面价值组合回测" in (out / "summary.md").read_text(encoding="utf-8")
