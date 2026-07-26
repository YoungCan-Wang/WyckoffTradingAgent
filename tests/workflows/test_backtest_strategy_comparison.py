from pathlib import Path

from workflows.backtest_strategy_comparison import (
    build_strategy_comparison,
    load_strategy_comparison_rows,
    render_strategy_comparison,
)
from workflows.backtest_strategy_variants import DEFAULT_COMPARISON_VARIANTS


def _write_summary(
    root: Path, period: str, variant: str, cash_return: float, drawdown: float, *, run_number: int | None = None
) -> None:
    suffix = f"-{run_number}" if run_number is not None else ""
    target = root / f"backtest-strategy-{period}-{variant}{suffix}"
    target.mkdir(parents=True)
    (target / "summary_fixture.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "- 区间: 2020-01-01 ~ 2020-06-30",
                "- 策略消融组: " + variant,
                "- 平均收益: 1.25%",
                "- 夏普比 (Sharpe Ratio): 1.5",
                "- 成交样本: 30",
                "## 真实现金账户模拟",
                f"- 总收益: {cash_return}%",
                f"- 现金最大回撤: {drawdown}%",
                "- 成交笔数: 24",
                "- 胜率: 55%",
            ]
        ),
        encoding="utf-8",
    )
    (target / "trades_fixture.csv").write_text(
        f"signal_date,code\n2020-01-02,{period}-{variant}\n",
        encoding="utf-8",
    )


def test_strategy_comparison_builds_relative_and_walk_forward_results(tmp_path: Path) -> None:
    periods = ["bull_2020", "bear_2022", "sideways_2023", "volatile_2024", "recent_6m"]
    for period_index, period in enumerate(periods):
        for variant_index, variant in enumerate(DEFAULT_COMPARISON_VARIANTS):
            _write_summary(tmp_path, period, variant, 2.0 + variant_index + period_index, -4.0)

    rows = load_strategy_comparison_rows(tmp_path)
    report = build_strategy_comparison(rows)

    assert len(rows) == 20
    assert report["status"] == "ready"
    assert report["evaluations"]["M"]["reference_variant"] == "A"
    assert report["evaluations"]["P"]["reference_variant"] == "M"
    assert report["evaluations"]["Q"]["reference_variant"] == "P"
    assert report["evaluations"]["P"]["status"] == "pass"
    assert report["evaluations"]["Q"]["status"] == "pass"
    assert report["evaluations"]["P"]["exposure_periods"] == 5
    assert report["evaluations"]["P"]["changed_trades"] == 10
    assert report["evaluations"]["Q"]["changed_trades"] == 10
    assert len(report["walk_forward"]["windows"]) == 4
    assert "相对参照组结论" in render_strategy_comparison(report)


def test_strategy_comparison_requires_every_period_variant_cell(tmp_path: Path) -> None:
    periods = ["bull_2020", "bear_2022", "sideways_2023", "recent_6m"]
    for period in periods:
        for variant in DEFAULT_COMPARISON_VARIANTS:
            _write_summary(tmp_path, period, variant, 2.0, -4.0)

    report = build_strategy_comparison(load_strategy_comparison_rows(tmp_path))

    assert report["status"] == "incomplete"
    assert set(report["missing_cells"]) == {
        "volatile_2024/A",
        "volatile_2024/M",
        "volatile_2024/P",
        "volatile_2024/Q",
    }


def test_strategy_comparison_accepts_github_artifact_run_suffix(tmp_path: Path) -> None:
    _write_summary(tmp_path, "recent_6m", "A", 2.0, -4.0, run_number=72)

    rows = load_strategy_comparison_rows(tmp_path)

    assert [(row.period, row.variant) for row in rows] == [("recent_6m", "A")]


def test_strategy_comparison_marks_identical_trade_sets_as_no_effect(tmp_path: Path) -> None:
    for period in ("bull_2020", "bear_2022", "recent_6m"):
        _write_summary(tmp_path, period, "A", 2.0, -4.0)
        _write_summary(tmp_path, period, "B", 3.0, -4.0)
        for variant in ("A", "B"):
            target = tmp_path / f"backtest-strategy-{period}-{variant}" / "trades_fixture.csv"
            target.write_text("signal_date,code\n2020-01-02,SAME\n", encoding="utf-8")

    report = build_strategy_comparison(load_strategy_comparison_rows(tmp_path))

    assert report["evaluations"]["B"]["status"] == "no_effect"
    assert report["evaluations"]["B"]["changed_trades"] == 0


def test_strategy_comparison_prefers_executed_cash_trades_for_exposure(tmp_path: Path) -> None:
    for period in ("bull_2020", "bear_2022", "recent_6m"):
        _write_summary(tmp_path, period, "A", 2.0, -4.0)
        _write_summary(tmp_path, period, "M", 3.0, -4.0)
        for variant in ("A", "M"):
            target = tmp_path / f"backtest-strategy-{period}-{variant}"
            (target / "cash_trades_fixture.csv").write_text(
                "signal_date,code,entry_weight_multiplier,exit_date\n2020-01-02,SAME,1.0,2020-01-15\n",
                encoding="utf-8",
            )

    report = build_strategy_comparison(load_strategy_comparison_rows(tmp_path))

    assert report["evaluations"]["M"]["status"] == "no_effect"
    assert report["evaluations"]["M"]["changed_trades"] == 0


def test_strategy_comparison_uses_executed_cash_trade_average(tmp_path: Path) -> None:
    _write_summary(tmp_path, "recent_6m", "A", 2.0, -4.0)
    target = tmp_path / "backtest-strategy-recent_6m-A" / "cash_trades_fixture.csv"
    target.write_text("ret_pct\n-5\n3\n", encoding="utf-8")

    row = load_strategy_comparison_rows(tmp_path)[0]

    assert row.avg_return == -1.0


def test_strategy_comparison_counts_position_weight_as_treatment_exposure(tmp_path: Path) -> None:
    for period in ("bull_2020", "bear_2022", "recent_6m"):
        _write_summary(tmp_path, period, "A", 2.0, -4.0)
        _write_summary(tmp_path, period, "M", 3.0, -4.0)
        baseline = tmp_path / f"backtest-strategy-{period}-A" / "trades_fixture.csv"
        candidate = tmp_path / f"backtest-strategy-{period}-M" / "trades_fixture.csv"
        baseline.write_text(
            "signal_date,code,entry_weight_multiplier\n2020-01-02,SAME,1.0\n",
            encoding="utf-8",
        )
        candidate.write_text(
            "signal_date,code,entry_weight_multiplier\n2020-01-02,SAME,0.5\n",
            encoding="utf-8",
        )

    report = build_strategy_comparison(load_strategy_comparison_rows(tmp_path))

    assert report["evaluations"]["M"]["exposure_periods"] == 3
    assert report["evaluations"]["M"]["changed_trades"] == 6


def test_strategy_comparison_counts_lower_weight_as_treatment_exposure(tmp_path: Path) -> None:
    for period in ("bull_2020", "bear_2022", "recent_6m"):
        _write_summary(tmp_path, period, "M", 2.0, -4.0)
        _write_summary(tmp_path, period, "P", 3.0, -4.0)
        baseline = tmp_path / f"backtest-strategy-{period}-M" / "trades_fixture.csv"
        candidate = tmp_path / f"backtest-strategy-{period}-P" / "trades_fixture.csv"
        baseline.write_text(
            "signal_date,code,entry_weight_multiplier,exit_date\n2020-01-02,SAME,1.0,2020-01-20\n",
            encoding="utf-8",
        )
        candidate.write_text(
            "signal_date,code,entry_weight_multiplier,exit_date\n2020-01-02,SAME,0.25,2020-01-20\n",
            encoding="utf-8",
        )

    report = build_strategy_comparison(load_strategy_comparison_rows(tmp_path))

    assert report["evaluations"]["P"]["exposure_periods"] == 3
    assert report["evaluations"]["P"]["changed_trades"] == 6


def test_strategy_comparison_rejects_profitable_delta_with_loss_or_excess_drawdown(tmp_path: Path) -> None:
    periods = ("bull_2020", "bear_2022", "recent_6m")
    for period in periods:
        _write_summary(tmp_path, period, "M", -5.0, -4.0)
        candidate_return, candidate_drawdown = (-1.0, -25.0) if period == "recent_6m" else (2.0, -4.0)
        _write_summary(tmp_path, period, "P", candidate_return, candidate_drawdown)

    report = build_strategy_comparison(load_strategy_comparison_rows(tmp_path))
    result = report["evaluations"]["P"]

    assert result["wins"] == 3
    assert result["mean_return_delta"] > 0
    assert result["positive_periods"] == 2
    assert result["max_abs_drawdown"] == 25.0
    assert result["status"] == "review"
