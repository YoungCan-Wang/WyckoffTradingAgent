from __future__ import annotations

from pathlib import Path

from workflows.backtest_market_report_artifacts import load_grid_cells
from workflows.backtest_walk_forward import build_walk_forward_validation

PERIODS = {
    "bull_2020": ("2020-07-01", "2021-02-18"),
    "bear_2022": ("2021-12-13", "2022-10-31"),
    "recent_6m": ("2026-01-01", "2026-06-30"),
}


def _cell(root: Path, period: str, hold: int, cash_return: float) -> None:
    start, end = PERIODS[period]
    artifact = root / f"backtest-grid-{period}-h{hold}-sl-7-tp0-tr0"
    artifact.mkdir()
    (artifact / f"summary_{period}_h{hold}.md").write_text(
        "\n".join(
            [
                f"- 区间: {start} ~ {end}",
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


def _grid(root: Path, returns: dict[int, tuple[float, float, float]]) -> None:
    for hold, values in returns.items():
        for period, value in zip(PERIODS, values, strict=True):
            _cell(root, period, hold, value)


def test_walk_forward_uses_train_winner_without_reoptimizing_test_period(tmp_path: Path) -> None:
    _grid(tmp_path, {10: (8.0, -2.0, 3.0), 15: (4.0, 6.0, 5.0)})

    result = build_walk_forward_validation(load_grid_cells(tmp_path))

    assert result["status"] == "fail"
    assert result["evaluated_window_count"] == 2
    assert result["windows"][0]["selected_params"]["hold_days"] == 10
    assert result["windows"][0]["test_cash_return"] == -2.0
    assert result["windows"][1]["selected_params"]["hold_days"] == 15
    assert result["windows"][1]["test_cash_return"] == 5.0


def test_walk_forward_passes_when_each_train_winner_survives_next_window(tmp_path: Path) -> None:
    _grid(tmp_path, {10: (8.0, 2.0, 3.0), 15: (4.0, 6.0, 5.0)})

    result = build_walk_forward_validation(load_grid_cells(tmp_path))

    assert result["status"] == "pass"
    assert result["positive_test_count"] == 2


def _unkeyed_cell(root: Path, hold: int, cash_return: float) -> None:
    """目录名不含已登记的 period_key，模拟报表侧名单漏改后的产物。"""
    artifact = root / f"backtest-grid-h{hold}-sl-7-tp0-tr0"
    artifact.mkdir()
    (artifact / f"summary_unknown_h{hold}.md").write_text(
        "\n".join(
            [
                "- 区间: 2025-01-02 ~ 2025-12-31",
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


def test_walk_forward_keeps_period_whose_key_cannot_be_resolved(tmp_path: Path) -> None:
    """period_key 解析不出来时按日期区间兜底，周期不能整段消失。

    旧实现是 ``if cell.period_key``：空 key 直接被过滤，该周期从时序链里消失且无日志。
    实测后果见 run 32537955220 —— bull_2025 在 16 个窗口里出现 0 次。
    """
    _grid(tmp_path, {10: (8.0, -2.0, 3.0), 15: (4.0, 6.0, 5.0)})
    _unkeyed_cell(tmp_path, 10, 12.0)
    _unkeyed_cell(tmp_path, 15, 9.0)

    cells = load_grid_cells(tmp_path)
    assert any(cell.period_key == "" for cell in cells), "用例前提：需要有解析不出 key 的单元"

    result = build_walk_forward_validation(cells)

    # 三个已登记周期 + 兜底周期 = 4 个，迁移窗口从 2 个增加到 3 个。
    assert result["evaluated_window_count"] == 3
    ends = [window["test_period"] for window in result["windows"]]
    assert any("2025-01-02" in str(end) for end in ends), ends
