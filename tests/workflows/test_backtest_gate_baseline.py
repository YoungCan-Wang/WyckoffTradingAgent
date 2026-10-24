from pathlib import Path

from workflows.backtest_gate_baseline import (
    BASELINE_PERIODS,
    build_gate_baseline,
    load_gate_baseline_rows,
    render_gate_baseline,
)

_LIVE_TRADES = [("2025-03-03", "000001", "CAUTION", -3.0, 1.0, 2.0)]
_OFF_TRADES = [
    ("2025-03-03", "000001", "CAUTION", -3.0, 1.0, 2.0),
    ("2025-03-04", "000002", "RISK_ON", 8.0, 1.0, 1.5),
    ("2025-03-05", "000003", "RISK_ON", 6.0, 0.5, 1.0),
    ("2025-03-06", "000004", "NEUTRAL", -1.0, 0.0, 0.5),
]


def _write_cell(root: Path, period: str, gate: str, trades: list[tuple], *, run_number: int | None = 7) -> None:
    suffix = f"-{run_number}" if run_number is not None else ""
    target = root / f"backtest-gate-{period}-{gate}{suffix}" / f"backtest-gate-{period}-{gate}"
    target.mkdir(parents=True)
    (target / "summary_fixture.md").write_text(
        "\n".join(
            [
                "# Wyckoff Funnel Daily Backtest",
                "- 区间: 2025-01-02 ~ 2025-12-31",
                "## 真实现金账户模拟",
                f"- 总收益: {'27.09' if gate == 'off' else '-11.83'}%",
                "- 现金最大回撤: -15.2%",
                f"- 成交笔数: {len(trades)}",
                "- 胜率: 50%",
            ]
        ),
        encoding="utf-8",
    )
    lines = ["signal_date,code,regime,ret_pct,bench_main_ret_pct,bench_smallcap_ret_pct"]
    lines.extend(",".join(str(value) for value in trade) for trade in trades)
    (target / "trades_fixture.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_gate_baseline_pairs_live_and_off_and_splits_marginal_trades_by_regime(tmp_path: Path) -> None:
    for period in BASELINE_PERIODS:
        _write_cell(tmp_path, period, "live", _LIVE_TRADES)
        _write_cell(tmp_path, period, "off", _OFF_TRADES)

    rows = load_gate_baseline_rows(tmp_path)
    report = build_gate_baseline(rows)

    assert len(rows) == 12
    assert report["status"] == "ready"
    off_cell = next(cell for cell in report["cells"] if cell["period"] == "bull_2025" and cell["gate"] == "off")
    assert off_cell["trades"] == 4
    assert off_cell["avg_return"] == 2.5
    assert off_cell["excess_main"]["avg"] == 1.875
    assert off_cell["excess_smallcap"]["beat_rate"] == 50.0
    assert off_cell["tail"]["drop_1"] == 2.0 / 3.0
    assert off_cell["cash_return"] == 27.09
    marginal = report["marginal"]["bull_2025"]
    assert marginal["common"]["trades"] == 1
    assert marginal["live_only"]["trades"] == 0
    assert marginal["off_only"]["trades"] == 3
    risk_on = next(row for row in report["off_only_by_regime"] if row["regime"] == "RISK_ON")
    assert risk_on["trades"] == 2 * len(BASELINE_PERIODS)
    assert risk_on["avg_return"] == 7.0
    assert risk_on["excess_smallcap_avg"] == 5.75
    assert report["pooled"]["off"]["trades"] == 4 * len(BASELINE_PERIODS)
    assert report["verdict"]["live"]["periods"] == len(BASELINE_PERIODS)
    markdown = render_gate_baseline(report)
    assert "仅 off 边际成交按水温" in markdown
    assert "| RISK_ON | 12 |" in markdown


def test_gate_baseline_reports_missing_cells_and_ignored_dirs(tmp_path: Path) -> None:
    _write_cell(tmp_path, "bull_2025", "off", _OFF_TRADES, run_number=None)
    stray = tmp_path / "backtest-grid-bull_2025-h15-sl8-tp18-tr0"
    stray.mkdir()
    (stray / "summary_x.md").write_text("- 区间: 2025-01-02 ~ 2025-12-31\n", encoding="utf-8")

    ignored: list[str] = []
    report = build_gate_baseline(load_gate_baseline_rows(tmp_path, ignored), ignored)

    assert report["status"] == "incomplete"
    assert "bull_2025/live" in report["missing_cells"]
    assert "bull_2020/off" in report["missing_cells"]
    assert report["ignored_dirs"] == ["backtest-grid-bull_2025-h15-sl8-tp18-tr0"]
    assert report["marginal"] == {}
    assert report["verdict"]["off"]["status"] == "review"
    assert "证据不完整" in render_gate_baseline(report)


def test_gate_baseline_signal_requires_both_benchmark_cis_to_exclude_zero() -> None:
    from workflows.backtest_gate_baseline import GateTrade, _protocol

    trades = [GateTrade(f"d:{idx}", "RISK_ON", 5.0 + idx * 0.1, 4.0 + idx * 0.1, -0.1) for idx in range(30)]
    assert _protocol(trades)["signal"] == "inconclusive"
    trades = [GateTrade(f"d:{idx}", "RISK_ON", 5.0 + idx * 0.1, 4.0 + idx * 0.1, 3.0 + idx * 0.1) for idx in range(30)]
    assert _protocol(trades)["signal"] == "positive"
    assert _protocol([])["signal"] == "inconclusive"
