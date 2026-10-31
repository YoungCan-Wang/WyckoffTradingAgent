from pathlib import Path

import pytest

from workflows.backtest_exit_baseline import (
    CELLS,
    LEADER_SPEC,
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


def _write_shard(
    root: Path,
    period: str,
    strategy: str,
    trades_by_cell: dict[str, list[tuple]],
    cash: float,
    *,
    prefix: str = "backtest-exit",
) -> None:
    shard = root / f"{prefix}-{period}-{strategy}-7" / f"{prefix}-{period}-{strategy}"
    shard.mkdir(parents=True)
    for key, (day, close) in {
        "main": (("20250102", 100.0), ("20251231", 110.0)),
        "smallcap": (("20250102", 100.0), ("20251231", 150.0)),
    }.items():
        (shard / f"benchmark_{key}.csv").write_text(
            "date,open,close\n" + "\n".join(f"{d},{c},{c}" for d, c in (day, close)) + "\n", encoding="utf-8"
        )
    for cell, trades in trades_by_cell.items():
        target = shard / _CELL_DIRS.get(cell, f"{prefix}-h{cell[1:].split('_')[0]}-sl0-tp0-tr0")
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
        _write_shard(tmp_path, period, "sector_rs", {cell: _trades(-9.0, "time_exit") for cell in CELLS}, cash=0.0)
    rows = load_exit_baseline_rows(tmp_path)
    assert len(rows) == 3 * len(CELLS) * len(BASELINE_PERIODS)
    report = build_exit_baseline(rows)
    assert report["status"] == "ready"
    assert report["missing_cells"] == []
    assert report["separation"]["h60_none"] == {"wyckoff": "above_random", "sector_rs": "below_random"}
    assert report["separation"]["h15_sl8_tp18"]["wyckoff"] == "indistinguishable"
    assert report["verdict"]["status"] == "positive_structure_found"
    assert report["verdict"]["positive_cells"] == ["wyckoff/h60_none"]
    pooled = report["pooled"]["wyckoff"]["h15_sl8_tp18"]
    assert pooled["trades"] == 30 * len(BASELINE_PERIODS)
    # 现金 +20% 对上证 +10% / 创业板 +50%：组合层超额 +10pp / −30pp，六个周期各自为正 / 为负。
    main = pooled["portfolio_excess"]["main"]
    assert (main["periods"], main["positive"]) == (6, 6)
    assert main["avg"] == pytest.approx(10.0)
    assert pooled["portfolio_excess"]["smallcap"]["positive"] == 0
    markdown = render_exit_baseline(report)
    assert "| h60_none | wyckoff |" in markdown and "above_random" in markdown
    assert "+10.00pp (6/6)" in markdown


def test_exit_baseline_marks_missing_cells_and_persistent_tax(tmp_path: Path) -> None:
    _write_shard(tmp_path, "bull_2020", "wyckoff", {"h15_sl8_tp18": _trades(-2.0, "stop_loss")}, cash=1.0)
    _write_shard(tmp_path, "bull_2020", "random", {"h15_sl8_tp18": _trades(-2.0, "stop_loss")}, cash=1.0)
    _write_shard(tmp_path, "bull_2020", "quality", {"h15_sl8_tp18": _trades(-2.0, "stop_loss")}, cash=1.0)
    report = build_exit_baseline(load_exit_baseline_rows(tmp_path))
    assert report["status"] == "incomplete"
    assert "wyckoff/bear_2022/h15_sl8_tp18" in report["missing_cells"]
    assert "quality/bear_2022/h15_sl8_tp18" in report["missing_cells"]
    # 对照组按矩阵里实际出现的算：没跑 sector_rs 不算缺格。
    assert not any(cell.startswith("sector_rs/") for cell in report["missing_cells"])
    assert report["separation"]["h15_sl8_tp18"] == {"wyckoff": "indistinguishable", "quality": "indistinguishable"}
    assert report["separation"]["h30_none"]["wyckoff"] == "insufficient"
    assert report["verdict"]["status"] == "execution_tax_persists"


def test_leader_spec_judges_leader_against_limit_random(tmp_path: Path) -> None:
    for period in BASELINE_PERIODS:
        kwargs = {"cash": 1.0, "prefix": LEADER_SPEC.prefix}
        _write_shard(tmp_path, period, "random", {c: _trades(-1.0, "time_exit") for c in LEADER_SPEC.cells}, **kwargs)
        _write_shard(
            tmp_path, period, "limit_random", {c: _trades(4.0, "time_exit") for c in LEADER_SPEC.cells}, **kwargs
        )
        _write_shard(
            tmp_path, period, "limit_leader", {c: _trades(4.05, "time_exit") for c in LEADER_SPEC.cells}, **kwargs
        )
    rows = load_exit_baseline_rows(tmp_path, LEADER_SPEC)
    assert len(rows) == 3 * len(LEADER_SPEC.cells) * len(BASELINE_PERIODS)
    report = build_exit_baseline(rows, LEADER_SPEC)
    assert report["status"] == "ready" and report["title"].startswith("涨停次日接力基线")
    # limit_random 对 random 分离；limit_leader 的参照臂是 limit_random，与之不可区分。
    assert report["separation"]["h1_none"] == {"limit_random": "above_random", "limit_leader": "indistinguishable"}
    assert report["verdict"]["positive_cells"] == [f"limit_random/{cell}" for cell in LEADER_SPEC.cells]
    assert "| h5_none | limit_leader |" in render_exit_baseline(report)
