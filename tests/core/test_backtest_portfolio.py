from __future__ import annotations

import pandas as pd

from core.backtest_portfolio import build_portfolio_nav, calc_portfolio_metrics
from workflows.backtest_portfolio import PortfolioBacktestRequest, run_portfolio_backtest


def _sample_trades() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "000001",
                "track": "SOS",
                "signal_date": "2026-02-01",
                "exit_date": "2026-02-03",
                "score": 80,
                "ret_pct": 10,
            },
            {
                "code": "000002",
                "track": "LPS",
                "signal_date": "2026-02-01",
                "exit_date": "2026-02-03",
                "score": 20,
                "ret_pct": -20,
            },
        ]
    )


def test_build_portfolio_nav_preserves_equal_cash_allocation() -> None:
    nav_df = build_portfolio_nav(_sample_trades(), initial_capital=1000, max_concurrent=2)

    assert nav_df["nav"].round(2).tolist() == [1000.0, 952.5]
    assert nav_df["cash"].round(2).tolist() == [50.0, 952.5]
    assert nav_df["positions_count"].tolist() == [2, 0]


def test_build_portfolio_nav_score_mode_ignores_invalid_scores() -> None:
    trades = pd.DataFrame(
        [
            {
                "code": "BAD",
                "signal_date": "2026-02-01",
                "exit_date": "2026-02-03",
                "score": "bad",
                "ret_pct": 100,
            },
            {
                "code": "INF",
                "signal_date": "2026-02-01",
                "exit_date": "2026-02-03",
                "score": float("inf"),
                "ret_pct": 100,
            },
            {
                "code": "GOOD",
                "signal_date": "2026-02-01",
                "exit_date": "2026-02-03",
                "score": 4.25,
                "ret_pct": 10,
            },
        ]
    )

    nav_df = build_portfolio_nav(trades, initial_capital=1000, max_concurrent=3, weight_mode="score")

    assert nav_df["nav"].round(2).tolist() == [1000.0, 1095.0]
    assert nav_df["cash"].round(2).tolist() == [50.0, 1095.0]
    assert nav_df["positions_count"].tolist() == [1, 0]


def test_calc_portfolio_metrics_reports_final_nav() -> None:
    nav_df = build_portfolio_nav(_sample_trades(), initial_capital=1000, max_concurrent=2)
    metrics = calc_portfolio_metrics(nav_df, initial_capital=1000)

    assert round(metrics["total_return_pct"], 3) == -4.75
    assert metrics["trading_days"] == 2
    assert metrics["final_nav"] == 952.5


def test_run_portfolio_backtest_writes_nav_and_summary(tmp_path) -> None:
    trades_path = tmp_path / "trades.csv"
    _sample_trades().to_csv(trades_path, index=False)

    result = run_portfolio_backtest(
        PortfolioBacktestRequest(trades_path=trades_path, output_dir=tmp_path / "out", initial_capital=1000)
    )

    assert result.trades_count == 2
    assert result.trading_days == 2
    assert result.nav_path.exists()
    assert result.summary_path.exists()
    assert "组合级回测结果" in result.summary_markdown
    assert "总收益: -4.750%" in result.summary_path.read_text(encoding="utf-8")
