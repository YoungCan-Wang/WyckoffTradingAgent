from __future__ import annotations

from datetime import date

import pandas as pd

from workflows.backtest_artifacts import (
    backtest_stamp,
    error_suite_row,
    success_suite_row,
    write_backtest_artifacts,
    write_suite_summary,
)


def test_write_backtest_artifacts_persists_private_frames(tmp_path) -> None:
    stamp = backtest_stamp(date(2026, 1, 1), date(2026, 1, 31), 10, 4)
    summary = _summary(
        {
            "_nav_df": pd.DataFrame([{"date": "2026-01-02", "nav": 1.01}]),
            "_cash_portfolio_trades_by_style": {"confirmation_only": pd.DataFrame([{"code": "000001"}])},
            "_wbt_pairs_df": pd.DataFrame([{"pair": "p1"}]),
        }
    )

    result = write_backtest_artifacts(
        out_dir=tmp_path,
        stamp=stamp,
        trades_df=pd.DataFrame([{"code": "000001", "ret_pct": 1.2}]),
        summary=summary,
    )

    assert result.summary_path.name == f"summary_{stamp}.md"
    assert result.trades_path.name == f"trades_{stamp}.csv"
    assert (tmp_path / f"nav_{stamp}.csv").exists()
    assert (tmp_path / f"cash_trades_confirmation_only_{stamp}.csv").exists()
    assert (tmp_path / f"wbt_pairs_{stamp}.csv").exists()
    assert "_nav_df" not in summary


def test_write_suite_summary_renders_rows_and_escapes_errors(tmp_path) -> None:
    rows = [
        success_suite_row(5, {"trades": 10, "win_rate_pct": 50.0, "cash_portfolio_final_cash": 101000.0}),
        error_suite_row(10, "bad|pipe"),
    ]

    paths = write_suite_summary(
        out_dir=tmp_path,
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 31),
        suite_rows=rows,
        success_count=1,
        candidate_mode="tradeable_l4",
    )

    assert paths is not None
    suite_md, suite_csv = paths
    assert suite_md.exists()
    assert suite_csv.exists()
    text = suite_md.read_text(encoding="utf-8")
    assert "mode=tradeable_l4" in text
    assert "bad/pipe" in text


def _summary(extra: dict | None = None) -> dict:
    base = {
        "start": "2026-01-01",
        "end": "2026-01-31",
        "hold_days": 10,
        "top_n": 4,
        "ai_top_n_cap": 4,
        "exit_mode": "sltp",
        "trailing_stop_pct": 0,
        "buy_friction_pct": 0.5,
        "sell_friction_pct": 0.5,
        "cash_portfolio_enabled": False,
        "wbt_requested": False,
    }
    if extra:
        base.update(extra)
    return base
