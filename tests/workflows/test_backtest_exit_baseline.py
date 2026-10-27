from pathlib import Path

import pytest

from workflows.backtest_exit_baseline import (
    CELLS,
    build_exit_baseline,
    load_exit_baseline_rows,
    render_exit_baseline,
)
from workflows.backtest_gate_baseline import BASELINE_PERIODS

_CELL_DIRS = {
    "h15_sl8_tp18": "backtest-exit-h15-sl8-tp18-tr0",
    "h15_none": "backtest-exit-h15-sl0-tp0-tr0",
    "h30_sl8_tp18": "backtest-exit-h30-sl8-tp18-tr0",
    "h30_none": "backtest-exit-h30-sl0-tp0-tr0",
    "h60_sl8_tp18": "backtest-exit-h60-sl8-tp18-tr0",
    "h60_none": "backtest-exit-h60-sl0-tp0-tr0",
}


def _trades(base: float, reason: str) -> list[tuple]:
    return [
        (f"2025-03-{day:02d}", f"00{day:04d}", "NEUTRAL", base + day * 0.1, 1.0, 1.5, reason) for day in range(1, 31)
    ]


def _write_shard(root: Path, period: str, strategy: str, trades_by_cell: dict[str, list[tuple]], cash: float) -> None:
    shard = root / f"backtest-exit-{period}-{strategy}-7" / f"backtest-exit-{period}-{strategy}"
    shard.mkdir(parents=True)
    for key, (day, close) in {
        "main": (("20250102", 100.0), ("20251231", 110.0)),
        "smallcap": (("20250102", 100.0), ("20251231", 150.0)),
    }.items():
        (shard / f"benchmark_{key}.csv").write_text(
            "date,open,close\n" + "\n".join(f"{d},{c},{c}" for d, c in (day, close)) + "\n", encoding="utf-8"
        )
    for cell, trades in trades_by_cell.items():
        target = shard / _CELL_DIRS[cell]
        target.mkdir()
        (target / "summary_x.md").write_text(
            "\n".join(
                [
                    "# Wyckoff Funnel Daily Backtest",
                    "- 区间: 2025-01-02 ~ 2025-12-31",
                    "## 真实现金账户模拟",
                    f"- 总收益: {cash}%",
                    "- 现金最大回撤: -10%",
                    f"- 成交笔数: {len(trades)}",
                ]
            ),
            encoding="utf-8",
        )
        lines = ["signal_date,code,regime,ret_pct,bench_main_ret_pct,bench_smallcap_ret_pct,exit_reason"]
        lines.extend(",".join(str(value) for value in trade) for trade in trades)
        (target / "trades_x.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_exit_baseline_reads_shards_and_finds_positive_structure(tmp_path: Path) -> None:
    for period in BASELINE_PERIODS:
        _write_shard(
            tmp_path,
            period,
            "wyckoff",
            {cell: _trades(12.0 if cell == "h60_none" else -2.0, "time_exit") for cell in CELLS},
            cash=20.0,
        )
        _write_shard(tmp_path, period, "random", {cell: _trades(-2.0, "stop_loss") for cell in CELLS}, cash=5.0)
    rows = load_exit_baseline_rows(tmp_path)
    assert len(rows) == 2 * len(CELLS) * len(BASELINE_PERIODS)
    report = build_exit_baseline(rows)
    assert report["status"] == "ready"
    assert report["missing_cells"] == []
    assert report["separation"]["h60_none"] == "wyckoff_above_random"
    assert report["separation"]["h15_sl8_tp18"] == "indistinguishable"
    assert report["verdict"]["status"] == "positive_structure_found"
    assert report["verdict"]["positive_cells"] == ["h60_none"]
    pooled = report["pooled"]["wyckoff"]["h15_sl8_tp18"]
    assert pooled["trades"] == 30 * len(BASELINE_PERIODS)
    # 现金 +20% 对上证 +10% / 创业板 +50%：组合层超额 +10pp / −30pp，六个周期各自为正 / 为负。
    main = pooled["portfolio_excess"]["main"]
    assert (main["periods"], main["positive"]) == (6, 6)
    assert main["avg"] == pytest.approx(10.0)
    assert pooled["portfolio_excess"]["smallcap"]["positive"] == 0
    markdown = render_exit_baseline(report)
    assert "| h60_none | wyckoff |" in markdown and "wyckoff_above_random" in markdown
    assert "+10.00pp (6/6)" in markdown


def test_exit_baseline_marks_missing_cells_and_persistent_tax(tmp_path: Path) -> None:
    _write_shard(tmp_path, "bull_2020", "wyckoff", {"h15_sl8_tp18": _trades(-2.0, "stop_loss")}, cash=1.0)
    _write_shard(tmp_path, "bull_2020", "random", {"h15_sl8_tp18": _trades(-2.0, "stop_loss")}, cash=1.0)
    report = build_exit_baseline(load_exit_baseline_rows(tmp_path))
    assert report["status"] == "incomplete"
    assert "wyckoff/bear_2022/h15_sl8_tp18" in report["missing_cells"]
    assert report["separation"]["h15_sl8_tp18"] == "indistinguishable"
    assert report["separation"]["h30_none"] == "insufficient"
    assert report["verdict"]["status"] == "execution_tax_persists"
