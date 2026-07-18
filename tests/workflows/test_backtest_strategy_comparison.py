from pathlib import Path

from workflows.backtest_strategy_comparison import (
    build_strategy_comparison,
    load_strategy_comparison_rows,
    render_strategy_comparison,
)


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
    periods = ["bull_2020", "bear_2022", "recent_6m"]
    for period_index, period in enumerate(periods):
        for variant_index, variant in enumerate(("A", "F", "G", "H", "I")):
            _write_summary(tmp_path, period, variant, 2.0 + variant_index + period_index, -4.0)

    rows = load_strategy_comparison_rows(tmp_path)
    report = build_strategy_comparison(rows)

    assert len(rows) == 15
    assert report["status"] == "ready"
    assert report["evaluations"]["I"]["status"] == "pass"
    assert report["evaluations"]["I"]["exposure_periods"] == 3
    assert report["evaluations"]["I"]["changed_trades"] == 6
    assert len(report["walk_forward"]["windows"]) == 2
    assert "相对 A 组结论" in render_strategy_comparison(report)


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
