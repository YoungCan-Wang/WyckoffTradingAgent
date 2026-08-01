from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from workflows.backtest_runner import parse_grid_cells, parse_strategy_variants, run_backtest_runner


@dataclass(frozen=True)
class _Artifact:
    summary_md: str
    summary_path: Path
    trades_path: Path


def test_run_backtest_runner_executes_hold_day_suite(monkeypatch, tmp_path) -> None:
    import workflows.backtest_runner as runner

    requests: list[int] = []
    suite: dict[str, object] = {}

    monkeypatch.setattr(
        runner,
        "run_backtest_request",
        lambda request, **_kwargs: requests.append(request.hold_days) or (pd.DataFrame(), {"hold": request.hold_days}),
    )
    monkeypatch.setattr(
        runner,
        "write_backtest_artifacts",
        lambda **_kwargs: _Artifact("summary", tmp_path / "summary.md", tmp_path / "trades.csv"),
    )
    monkeypatch.setattr(runner, "success_suite_row", lambda hold_days, summary: {"hold_days": hold_days, **summary})
    monkeypatch.setattr(runner, "write_suite_summary", lambda **kwargs: suite.update(kwargs))

    result = run_backtest_runner(_args(tmp_path, hold_days_list="5,10"), progress=lambda *_args, **_kwargs: None)

    assert result == 0
    assert requests == [5, 10]
    assert suite["success_count"] == 2
    assert [row["hold_days"] for row in suite["suite_rows"]] == [5, 10]


def test_run_backtest_runner_reuses_signal_suite(monkeypatch, tmp_path) -> None:
    import workflows.backtest_runner as runner

    requests = []
    output_dirs: list[str] = []
    monkeypatch.setattr(
        runner,
        "run_backtest_request_suite",
        lambda items, **_kwargs: (
            requests.extend(items) or [(pd.DataFrame(), {"hold": item.hold_days}) for item in items]
        ),
    )
    monkeypatch.setattr(
        runner,
        "write_backtest_artifacts",
        lambda **kwargs: (
            output_dirs.append(str(kwargs["out_dir"]))
            or _Artifact("summary", tmp_path / "summary.md", tmp_path / "trades.csv")
        ),
    )
    monkeypatch.setattr(runner, "success_suite_row", lambda hold_days, summary: {"hold_days": hold_days, **summary})

    result = run_backtest_runner(
        _args(tmp_path, grid_cells="5:0:0:0,10:-7:18:0", grid_prefix="backtest-grid-recent_6m"),
        progress=lambda *_args, **_kwargs: None,
    )

    assert result == 0
    assert [(item.hold_days, item.stop_loss_pct, item.take_profit_pct) for item in requests] == [
        (5, 0.0, 0.0),
        (10, -7.0, 18.0),
    ]
    assert output_dirs[0].endswith("backtest-grid-recent_6m-h5-sl0-tp0-tr0")
    assert output_dirs[1].endswith("backtest-grid-recent_6m-h10-sl7-tp18-tr0")


def test_run_backtest_runner_reuses_signal_ledger_across_weight_variants(monkeypatch, tmp_path) -> None:
    import workflows.backtest_runner as runner

    requests = []
    output_dirs: list[str] = []
    monkeypatch.setattr(
        runner,
        "run_backtest_request_suite",
        lambda items, **_kwargs: (
            requests.extend(items) or [(pd.DataFrame(), {"variant": item.strategy_variant}) for item in items]
        ),
    )
    monkeypatch.setattr(
        runner,
        "write_backtest_artifacts",
        lambda **kwargs: (
            output_dirs.append(str(kwargs["out_dir"]))
            or _Artifact("summary", tmp_path / "summary.md", tmp_path / "trades.csv")
        ),
    )
    monkeypatch.setattr(runner, "success_suite_row", lambda hold_days, summary: {"hold_days": hold_days, **summary})

    result = run_backtest_runner(
        _args(
            tmp_path,
            strategy_variants="A,M,P",
            strategy_prefix="backtest-strategy-recent_6m",
        ),
        progress=lambda *_args, **_kwargs: None,
    )

    assert result == 0
    assert [request.strategy_variant for request in requests] == ["A", "M", "P"]
    assert [Path(path).name for path in output_dirs] == [
        "backtest-strategy-recent_6m-A",
        "backtest-strategy-recent_6m-M",
        "backtest-strategy-recent_6m-P",
    ]


def test_parse_strategy_variants_rejects_single_variant() -> None:
    with pytest.raises(ValueError, match="至少需要两个"):
        parse_strategy_variants("A")


def test_parse_grid_cells_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="非法 grid cell"):
        parse_grid_cells("10:7:18:0")


def test_trigger_grid_reruns_full_funnel_per_threshold(monkeypatch, tmp_path) -> None:
    import workflows.backtest_runner as runner

    overrides: list[tuple] = []
    output_dirs: list[str] = []
    monkeypatch.setattr(
        runner,
        "run_backtest_request",
        lambda request, **_kwargs: (
            overrides.append(request.funnel_overrides)
            or (pd.DataFrame(), {"stratified": {"by_trigger": {"spring(确认)": {"trades": 30, "avg_ret_pct": 1.0}}}})
        ),
    )
    monkeypatch.setattr(
        runner,
        "write_backtest_artifacts",
        lambda **kwargs: (
            output_dirs.append(str(kwargs["out_dir"]))
            or _Artifact("summary", tmp_path / "summary.md", tmp_path / "trades.csv")
        ),
    )
    monkeypatch.setattr(runner, "success_suite_row", lambda hold_days, summary: {"hold_days": hold_days})

    result = run_backtest_runner(
        _args(
            tmp_path,
            trigger_grid="spring_vol_ratio=1.1,1.5",
            grid_prefix="backtest-trigger-bear_2022",
            period_key="bear_2022",
        ),
        progress=lambda *_args, **_kwargs: None,
    )

    assert result == 0
    assert overrides == [(("spring_vol_ratio", 1.1),), (("spring_vol_ratio", 1.5),)]
    assert output_dirs[0].endswith("backtest-trigger-bear_2022-spring_vol_ratio-1.1")
    matrix = json.loads((tmp_path / "trigger_matrix_spring_vol_ratio.json").read_text(encoding="utf-8"))
    assert [row["value"] for row in matrix["rows"]] == [1.1, 1.5]
    assert {row["period_key"] for row in matrix["rows"]} == {"bear_2022"}


def test_top_n_grid_sweeps_selection_instead_of_funnel_overrides(monkeypatch, tmp_path) -> None:
    import workflows.backtest_runner as runner

    requests: list = []
    monkeypatch.setattr(
        runner,
        "run_backtest_request",
        lambda request, **_kwargs: requests.append(request) or (pd.DataFrame(), {"trades": 900, "avg_ret_pct": -1.0}),
    )
    monkeypatch.setattr(
        runner,
        "write_backtest_artifacts",
        lambda **_kwargs: _Artifact("summary", tmp_path / "summary.md", tmp_path / "trades.csv"),
    )
    monkeypatch.setattr(runner, "success_suite_row", lambda hold_days, summary: {"hold_days": hold_days})

    result = run_backtest_runner(
        _args(tmp_path, trigger_grid="top_n=0,1", period_key="bear_2022"),
        progress=lambda *_args, **_kwargs: None,
    )

    assert result == 0
    assert [request.top_n for request in requests] == [0, 1]
    assert all(request.funnel_overrides == () for request in requests)
    matrix = json.loads((tmp_path / "trigger_matrix_top_n.json").read_text(encoding="utf-8"))
    assert [row["overall_avg_ret_pct"] for row in matrix["rows"]] == [-1.0, -1.0]


def _args(tmp_path: Path, **overrides) -> Namespace:
    values = {
        "start": "2026-01-01",
        "end": "2026-01-31",
        "output_dir": str(tmp_path),
        "hold_days": 10,
        "hold_days_list": "",
        "top_n": 0,
        "board": "all",
        "sample_size": 0,
        "trading_days": 320,
        "workers": 1,
        "snapshot_dir": "",
        "benchmark": "000001",
        "exit_mode": "close_only",
        "stop_loss": -9.0,
        "take_profit": 0.0,
        "trailing_stop": 0.0,
        "trailing_activate": 0.0,
        "sltp_priority": "stop_first",
        "use_current_meta": True,
        "buy_friction_pct": 0.0,
        "sell_friction_pct": 0.0,
        "regime_filter": False,
        "execution_regime_gate": "live",
        "pending_mode": "both",
        "pending_merge_order": "funnel_first",
        "atr_period": 14,
        "atr_multiplier": 2.0,
        "atr_hard_stop": -9.0,
        "metrics_engine": "legacy",
        "wbt_fee_rate": 0.0,
        "wbt_n_jobs": 1,
        "abc_filter": False,
        "entry_price_mode": "open",
        "entry_price_time": "14:55",
        "entry_price_fallback": "close",
        "cash_portfolio": False,
        "initial_cash": 100000.0,
        "max_positions": 4,
        "commission_rate": 0.0003,
        "min_commission": 5.0,
        "stamp_duty_rate": 0.0005,
        "transfer_fee_rate": 0.00001,
        "lot_size": 100,
        "portfolio_styles": "slot_equal_4",
        "trigger_grid": "",
        "period_key": "",
        "strategy_variants": "",
        "strategy_prefix": "backtest-strategy",
    }
    values.update(overrides)
    return Namespace(**values)
