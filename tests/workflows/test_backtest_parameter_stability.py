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


def _grid(root: Path, returns: dict[tuple[int, int], tuple[float, float, float]]) -> None:
    periods = ("recent_6m", "bull_2020", "bear_2022")
    for (hold, stop), values in returns.items():
        for period, value in zip(periods, values, strict=True):
            _cell(root, period, hold, stop, value)


def test_parameter_stability_passes_when_half_of_neighbors_are_cross_period_positive(tmp_path):
    _grid(
        tmp_path,
        {
            (15, 8): (6.0, 5.0, 4.0),
            (10, 8): (4.0, 3.0, 2.0),
            (15, 7): (3.0, 2.0, -1.0),
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
            (15, 8): (6.0, 5.0, 4.0),
            (10, 8): (4.0, -3.0, -2.0),
            (15, 7): (3.0, -2.0, -1.0),
        },
    )

    result = build_parameter_stability(load_grid_cells(tmp_path))

    assert result["status"] == "fail"
    assert "参数孤岛" in result["summary"]


def test_parameter_stability_reviews_insufficient_neighbor_coverage(tmp_path):
    _grid(tmp_path, {(15, 8): (6.0, 5.0, 4.0), (10, 8): (4.0, 3.0, 2.0)})

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


def test_parse_params_keeps_trailing_activate_distinct():
    """tr8 / tr8-ta5 / tr8-ta7 必须是三个不同的 param key。

    此前 _parse_params 不解析 ta 段，三者折叠成同一 key，参数稳定性验证器只评到
    其中一档——实测 run 31366326715 的 anchor ta 字段是 None，即只评了 activate=0，
    而那恰是三档里配对 t 最低的（ta0 +1.46 / ta5 +1.98 / ta7 +2.36）。
    """
    from workflows.backtest_market_report_artifacts import _parse_params

    keys = {_parse_params(f"backtest-grid-bull_2020-h10-sl8-tp0-tr8{suffix}") for suffix in ("", "-ta5", "-ta7")}

    assert keys == {(10, 8, 0, 8, 0), (10, 8, 0, 8, 5), (10, 8, 0, 8, 7)}


def test_parse_period_key_covers_all_matrix_periods():
    """周期名单需与 backtest_grid.yml 的 matrix 同步。

    缺 sideways_2023 / volatile_2024 时它们的 period_key 为空串，会回落到 start_end
    兜底：周期数不丢，但 REQUIRED_PERIODS 判定与 _representative 的偏好选择失准。
    """
    from workflows.backtest_market_report_artifacts import _parse_period_key

    for period in ("recent_2m", "recent_6m", "bull_2020", "bear_2022", "sideways_2023", "volatile_2024"):
        assert _parse_period_key(f"backtest-grid-{period}-h10-sl8-tp0-tr8") == period
