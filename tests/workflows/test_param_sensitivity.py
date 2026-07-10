from __future__ import annotations

import pandas as pd

from workflows.param_sensitivity import (
    ParamSensitivityRequest,
    _build_sensitivity_combos,
    build_sensitivity_markdown,
    run_param_sensitivity_request,
)


def test_build_sensitivity_combos_expands_grid() -> None:
    combos = _build_sensitivity_combos(([5, 10], [-6.0], [0.0], [0.0], [0.0], [3, 5]))

    assert [combo.label for combo in combos] == [
        "hd=5_sl=-6.0_tp=0.0_ts=0.0_ta=0.0_tn=3",
        "hd=5_sl=-6.0_tp=0.0_ts=0.0_ta=0.0_tn=5",
        "hd=10_sl=-6.0_tp=0.0_ts=0.0_ta=0.0_tn=3",
        "hd=10_sl=-6.0_tp=0.0_ts=0.0_ta=0.0_tn=5",
    ]


def test_build_sensitivity_markdown_reports_best_sharpe() -> None:
    df = pd.DataFrame(
        [
            {
                "hold_days": 5,
                "stop_loss_pct": -6.0,
                "take_profit_pct": 0.0,
                "top_n": 3,
                "trades": 8,
                "sharpe_ratio": 0.2,
            },
            {
                "hold_days": 10,
                "stop_loss_pct": -8.0,
                "take_profit_pct": 18.0,
                "trailing_stop_pct": 0.0,
                "trailing_activate_pct": 0.0,
                "top_n": 5,
                "trades": 10,
                "win_rate_pct": 60.0,
                "avg_ret_pct": 2.5,
                "max_drawdown_pct": -5.0,
                "sharpe_ratio": 1.2,
                "calmar_ratio": 0.7,
            },
        ]
    )

    markdown = build_sensitivity_markdown(df)

    assert "## 最优参数（按夏普比）" in markdown
    assert "hold_days: **10**" in markdown
    assert "take_profit: **18.0%**" in markdown


def test_run_param_sensitivity_request_writes_outputs(monkeypatch, tmp_path) -> None:
    import workflows.param_sensitivity as workflow

    monkeypatch.setattr(
        workflow,
        "run_sensitivity",
        lambda *_args, **_kwargs: pd.DataFrame(
            [
                {
                    "trades": 1,
                    "sharpe_ratio": 0.8,
                    "win_rate_pct": 60.0,
                    "avg_ret_pct": 2.5,
                    "hold_days": 10,
                    "stop_loss_pct": -8.0,
                    "take_profit_pct": 15.0,
                    "trailing_stop_pct": 0.0,
                    "trailing_activate_pct": 0.0,
                    "top_n": 3,
                }
            ]
        ),
    )

    result = run_param_sensitivity_request(
        ParamSensitivityRequest(start="2026-01-01", end="2026-01-31", output_dir=str(tmp_path))
    )

    assert result == 0
    assert len(list(tmp_path.glob("sensitivity_*.csv"))) == 1
    assert len(list(tmp_path.glob("sensitivity_*.md"))) == 1
