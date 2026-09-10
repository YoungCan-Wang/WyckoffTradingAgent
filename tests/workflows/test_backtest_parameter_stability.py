from __future__ import annotations

from pathlib import Path

from workflows.backtest_market_report_artifacts import load_grid_cells
from workflows.backtest_parameter_stability import build_parameter_stability


def _cell(root: Path, period: str, hold: int, stop: int, cash_return: float, win_rate: float = 50.0) -> None:
    artifact = root / f"backtest-grid-{period}-h{hold}-sl-{stop}-tp0-tr0"
    artifact.mkdir()
    (artifact / f"summary_{period}_h{hold}.md").write_text(
        "\n".join(
            [
                "- 区间: 2025-01-01 ~ 2025-06-30",
                "- 每日候选上限: Top 4",
                "- 股票池: all (sample=0)",
                "- 成交样本: 20",
                f"- 胜率: {win_rate}%",
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


def test_score_payload_reports_win_rate_span_across_periods(tmp_path):
    """payload 要带跨周期胜率跨度,不能只有现金收益。

    实测 09-09 网格的榜首代表格子胜率 53.85%,而它最差那个周期只有 27.59%——只报代表
    格子会把这段塌陷藏掉。这里三个周期给 60/40/30,断言 min 取到 30 而不是代表格子的值。
    """
    periods = ("recent_6m", "bull_2020", "bear_2022")
    for period, cash, win in zip(periods, (6.0, 5.0, 4.0), (60.0, 40.0, 30.0), strict=True):
        _cell(tmp_path, period, 15, 8, cash, win_rate=win)
        _cell(tmp_path, period, 10, 8, cash - 2, win_rate=win)
        _cell(tmp_path, period, 15, 7, cash - 3, win_rate=win)

    anchor = build_parameter_stability(load_grid_cells(tmp_path))["anchor"]

    assert anchor["min_win_rate_pct"] == 30.0
    assert anchor["avg_win_rate_pct"] == round((60.0 + 40.0 + 30.0) / 3, 4)
    # 现金收益列必须原样保留:胜率是新增对照,不是替换口径。
    assert anchor["min_cash_return"] == 4.0


def test_win_rate_columns_do_not_change_the_stability_verdict(tmp_path):
    """胜率只做展示,不进判定:现金收益一样、胜率天差地别的两个网格必须同一个结论。

    判定口径一改,历史上所有 pass/fail 的含义就变了一次。这条用例就是钉住「没改」。
    """
    high, low = tmp_path / "high", tmp_path / "low"
    high.mkdir()
    low.mkdir()
    periods = ("recent_6m", "bull_2020", "bear_2022")
    for root, win in ((high, 70.0), (low, 20.0)):
        for period, cash in zip(periods, (6.0, 5.0, 4.0), strict=True):
            _cell(root, period, 15, 8, cash, win_rate=win)
            _cell(root, period, 10, 8, cash - 2, win_rate=win)
            _cell(root, period, 15, 7, cash - 3, win_rate=win)

    high_result = build_parameter_stability(load_grid_cells(high))
    low_result = build_parameter_stability(load_grid_cells(low))

    for field in ("status", "neighbor_count", "stable_neighbor_count", "stable_neighbor_ratio"):
        assert high_result[field] == low_result[field], field
    assert high_result["anchor"]["robust_score"] == low_result["anchor"]["robust_score"]
    # 而胜率列本身必须跟着变,否则这条用例就退化成「两个空值相等」。
    assert high_result["anchor"]["min_win_rate_pct"] == 70.0
    assert low_result["anchor"]["min_win_rate_pct"] == 20.0


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
