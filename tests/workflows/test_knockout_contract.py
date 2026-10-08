from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from workflows import backtest
from workflows.backtest_strategy_comparison import _DIR_PATTERN, StrategyComparisonRow, build_strategy_comparison


def make_row(variant):
    return StrategyComparisonRow(
        "bull_2025", variant, "2025-01-01", "2025-12-31", 1, -1, 1, 50, 1, 1, signal_trades=(("2025-01-02|000001", 1),)
    )


def test_live_comparison_uses_correct_reference_and_marks_missing_cells():
    rows = [make_row("live"), make_row("KO_AMBUSH")]
    report = build_strategy_comparison(
        rows,
        reference_variant="live",
        required_variants=("live", "KO_AMBUSH", "KO_ACCUM"),
        required_periods=("bull_2025",),
    )
    assert report["baseline"] == "live"
    assert report["status"] == "incomplete"
    assert report["missing_cells"] == ["bull_2025/KO_ACCUM"]
    assert report["evaluations"]["live"]["status"] == "baseline"
    assert report["evaluations"]["KO_AMBUSH"]["reference_variant"] == "live"
    assert "bull_2025" in report["evaluations"]["KO_AMBUSH"]["marginal_by_period"]


@pytest.mark.parametrize("variant", ["live", "KO_AMBUSH", "KO_ACCUM", "KO_DRY_VOL", "KO_TREND_CONT", "Q", "R"])
def test_report_directory_recognizes_registered_variants(variant):
    assert _DIR_PATTERN.fullmatch(f"backtest-strategy-bull_2025-{variant}")


def test_production_replay_rejects_old_snapshot_for_knockout(monkeypatch, tmp_path):
    history = SimpleNamespace(all_df_map={"000001": pd.DataFrame({"date": [date(2026, 9, 30)], "turnover": [0]})})
    monkeypatch.setattr(backtest, "load_backtest_history", lambda **kw: history)
    monkeypatch.setattr(backtest, "load_snapshot_pit_meta", lambda path: {})
    request = SimpleNamespace(
        start_dt=date(2026, 9, 1),
        end_dt=date(2026, 9, 30),
        trading_days=320,
        hold_days=15,
        benchmark="000001",
        max_workers=1,
        strategy_variant="KO_AMBUSH",
    )
    with pytest.raises(ValueError, match="旧快照"):
        backtest._load_prepared_data(
            request, SimpleNamespace(snapshot_dir=tmp_path), SimpleNamespace(symbols=["000001"]), lambda *args: None
        )


def test_manual_workflow_keeps_legacy_default_and_isolates_knockouts():
    from pathlib import Path

    workflow = yaml.load(
        (Path(__file__).resolve().parents[2] / ".github/workflows/backtest_grid.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["strategy_compare_set"]["default"] == "legacy"
    assert inputs["run_strategy_compare"]["default"] == "false"
    assert (
        '"live","KO_AMBUSH","KO_ACCUM","KO_DRY_VOL","KO_TREND_CONT"'
        in workflow["jobs"]["strategy_compare"]["strategy"]["matrix"]["variant_set"]
    )
    assert workflow["permissions"] == {"contents": "read"}
    assert "BACKTEST_REQUIRE_PIT_TURNOVER" in workflow["jobs"]["strategy_compare"]["env"]
    assert "knockout" in workflow["jobs"]["grid"]["if"]
