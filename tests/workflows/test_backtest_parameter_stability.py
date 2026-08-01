from __future__ import annotations

from pathlib import Path

from workflows.backtest_market_report_artifacts import load_grid_cells
from workflows.backtest_parameter_stability import build_parameter_stability


def _cell(root: Path, period: str, hold: int, stop: int, cash_return: float) -> None:
    artifact = root / f"backtest-grid-{period}-h{hold}-sl-{stop}-tp0-tr0"
    artifact.mkdir()
    (artifact / f"summary_{period}_h{hold}.md").write_text(
        "\n".join(
            [
                "- 区间: 2025-01-01 ~ 2025-06-30",
                "- 每日候选上限: Top 4",
                "- 股票池: all (sample=0)",
                "- 成交样本: 20",
                "- 胜率: 50%",
                "- 初始现金: 100000",
                f"- 最终现金: {100000 * (1 + cash_return / 100):.2f}",
                f"- 总收益: {cash_return}%",
                "- 成交笔数: 10",
            ]
        ),
        encoding="utf-8",
    )


def _grid(root: Path, returns: dict[tuple[int, int], tuple[float, ...]]) -> None:
    periods = ("recent_6m", "bull_2020", "bear_2022", "sideways_2023", "volatile_2024")
    for (hold, stop), values in returns.items():
        for period, value in zip(periods, values, strict=True):
            _cell(root, period, hold, stop, value)


def test_parameter_stability_passes_when_half_of_neighbors_are_cross_period_positive(tmp_path):
    _grid(
        tmp_path,
        {
            (15, 8): (6.0, 5.0, 4.0, 3.0, 2.0),
            (10, 8): (4.0, 3.0, 2.0, 1.0, 0.5),
            (15, 7): (3.0, 2.0, -1.0, 1.0, 0.5),
        },
    )

    result = build_parameter_stability(load_grid_cells(tmp_path))

    assert result["status"] == "pass"
    assert result["neighbor_count"] == 2
    assert result["stable_neighbor_count"] == 1
    assert result["stable_neighbor_ratio"] == 0.5
    assert result["anchor"]["hold_days"] == 15


def test_parameter_stability_fails_for_parameter_island(tmp_path):
    _grid(
        tmp_path,
        {
            (15, 8): (6.0, 5.0, 4.0, 3.0, 2.0),
            (10, 8): (4.0, -3.0, -2.0, -1.0, -0.5),
            (15, 7): (3.0, -2.0, -1.0, -0.5, -0.25),
        },
    )

    result = build_parameter_stability(load_grid_cells(tmp_path))

    assert result["status"] == "fail"
    assert "参数孤岛" in result["summary"]


def test_parameter_stability_reviews_insufficient_neighbor_coverage(tmp_path):
    _grid(
        tmp_path,
        {
            (15, 8): (6.0, 5.0, 4.0, 3.0, 2.0),
            (10, 8): (4.0, 3.0, 2.0, 1.0, 0.5),
        },
    )

    result = build_parameter_stability(load_grid_cells(tmp_path))

    assert result["status"] == "review"
    assert result["neighbor_count"] == 1


def test_parameter_stability_reviews_when_anchor_misses_required_period(tmp_path):
    for period, value in (("recent_6m", 8.0), ("bull_2020", 7.0)):
        _cell(tmp_path, period, 15, 8, value)
        _cell(tmp_path, period, 10, 8, value - 1)
        _cell(tmp_path, period, 15, 7, value - 2)

    result = build_parameter_stability(load_grid_cells(tmp_path))

    assert result["status"] == "review"
    assert "bear_2022" in result["summary"]
