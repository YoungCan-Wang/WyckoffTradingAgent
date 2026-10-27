from pathlib import Path

from workflows.backtest_gate_baseline import (
    BASELINE_PERIODS,
    GateTrade,
    _contrast_status,
    _protocol,
    build_gate_baseline,
    load_gate_baseline_rows,
    render_gate_baseline,
)

_LIVE_TRADES = [("2025-03-03", "000001", "CAUTION", -3.0, 1.0, 2.0, "stop_loss")]
_OFF_TRADES = [
    ("2025-03-03", "000001", "CAUTION", -3.0, 1.0, 2.0, "stop_loss"),
    ("2025-03-04", "000002", "RISK_ON", 8.0, 1.0, 1.5, "take_profit"),
    ("2025-03-05", "000003", "RISK_ON", 6.0, 0.5, 1.0, "time_exit"),
    ("2025-03-06", "000004", "NEUTRAL", -1.0, 0.0, 0.5, "time_exit"),
]
_RS_TRADES = [
    (f"2025-03-{day:02d}", f"60{day:04d}", "RISK_ON", 12.0 + day * 0.1, 1.0, 1.5, "take_profit") for day in range(1, 31)
]
_RANDOM_TRADES = [
    (f"2025-03-{day:02d}", f"30{day:04d}", "NEUTRAL", -4.0 - day * 0.1, 1.0, 1.5, "stop_loss") for day in range(1, 31)
]


def _write_cell(
    root: Path,
    period: str,
    gate: str,
    trades: list[tuple],
    *,
    strategy: str | None = None,
    run_number: int | None = 7,
) -> None:
    name = f"backtest-gate-{period}-{gate}" + (f"-{strategy}" if strategy else "")
    suffix = f"-{run_number}" if run_number is not None else ""
    target = root / f"{name}{suffix}" / name
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
    lines = ["signal_date,code,regime,ret_pct,bench_main_ret_pct,bench_smallcap_ret_pct,exit_reason"]
    lines.extend(",".join(str(value) for value in trade) for trade in trades)
    (target / "trades_fixture.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_gate_baseline_pairs_live_and_off_and_splits_marginal_trades_by_regime(tmp_path: Path) -> None:
    for period in BASELINE_PERIODS:
        _write_cell(tmp_path, period, "live", _LIVE_TRADES)
        _write_cell(tmp_path, period, "off", _OFF_TRADES, strategy="wyckoff")
        _write_cell(tmp_path, period, "live", _RS_TRADES, strategy="rs")
        _write_cell(tmp_path, period, "off", _RS_TRADES, strategy="rs")
        _write_cell(tmp_path, period, "live", _RANDOM_TRADES, strategy="random")
        _write_cell(tmp_path, period, "off", _RANDOM_TRADES, strategy="random")
        _write_cell(tmp_path, period, "live", _RS_TRADES, strategy="reversal")
        _write_cell(tmp_path, period, "off", _RS_TRADES, strategy="reversal")

    rows = load_gate_baseline_rows(tmp_path)
    report = build_gate_baseline(rows)

    assert len(rows) == 48
    assert report["status"] == "ready"
    off_cell = next(
        cell
        for cell in report["cells"]
        if cell["period"] == "bull_2025" and cell["gate"] == "off" and cell["strategy"] == "wyckoff"
    )
    assert off_cell["trades"] == 4
    assert off_cell["avg_return"] == 2.5
    assert off_cell["excess_main"]["avg"] == 1.875
    assert off_cell["excess_smallcap"]["beat_rate"] == 50.0
    assert off_cell["tail"]["drop_1"] == 2.0 / 3.0
    assert off_cell["cash_return"] == 27.09
    assert off_cell["exit_reasons"] == {"stop_loss": 25.0, "take_profit": 25.0, "time_exit": 50.0}
    marginal = report["marginal"]["wyckoff"]["bull_2025"]
    assert marginal["common"]["trades"] == 1
    assert marginal["live_only"]["trades"] == 0
    assert marginal["off_only"]["trades"] == 3
    risk_on = next(row for row in report["off_only_by_regime"]["wyckoff"] if row["regime"] == "RISK_ON")
    assert risk_on["trades"] == 2 * len(BASELINE_PERIODS)
    assert risk_on["avg_return"] == 7.0
    assert risk_on["excess_smallcap_avg"] == 5.75
    assert report["pooled"]["wyckoff"]["off"]["trades"] == 4 * len(BASELINE_PERIODS)
    assert report["pooled"]["rs"]["off"]["trades"] == 30 * len(BASELINE_PERIODS)
    assert report["verdict"]["wyckoff"]["live"]["periods"] == len(BASELINE_PERIODS)
    assert report["verdict"]["rs"]["off"]["status"] == "review"
    contrast = report["strategy_contrast"]["off"]
    assert contrast["status"] == {
        "rs": "wyckoff_below_rs",
        "random": "wyckoff_above_random",
        "reversal": "wyckoff_below_reversal",
    }
    assert contrast["pooled"]["rs"]["signal"] == "positive"
    assert contrast["pooled"]["random"]["signal"] == "negative"
    assert contrast["rows"][0]["wyckoff"]["trades"] == 4
    markdown = render_gate_baseline(report)
    assert "wyckoff 仅 off 边际成交按水温" in markdown
    assert "| RISK_ON | 12 |" in markdown
    assert (
        "### 闸门 off → vs rs: wyckoff_below_rs；vs random: wyckoff_above_random；vs reversal: wyckoff_below_reversal"
        in markdown
    )
    assert "| 25% / 25% / 50% |" in markdown


def test_gate_baseline_reports_missing_cells_and_ignored_dirs(tmp_path: Path) -> None:
    _write_cell(tmp_path, "bull_2025", "off", _OFF_TRADES, run_number=None)
    stray = tmp_path / "backtest-grid-bull_2025-h15-sl8-tp18-tr0"
    stray.mkdir()
    (stray / "summary_x.md").write_text("- 区间: 2025-01-02 ~ 2025-12-31\n", encoding="utf-8")

    ignored: list[str] = []
    report = build_gate_baseline(load_gate_baseline_rows(tmp_path, ignored), ignored)

    assert report["status"] == "incomplete"
    assert "wyckoff/bull_2025/live" in report["missing_cells"]
    assert "rs/bull_2020/off" in report["missing_cells"]
    assert report["ignored_dirs"] == ["backtest-grid-bull_2025-h15-sl8-tp18-tr0"]
    assert report["marginal"] == {"wyckoff": {}, "rs": {}, "random": {}, "reversal": {}}
    assert report["verdict"]["wyckoff"]["off"]["status"] == "review"
    assert report["verdict"]["rs"]["off"]["status"] == "insufficient"
    assert report["strategy_contrast"]["off"]["status"] == {
        "rs": "insufficient",
        "random": "insufficient",
        "reversal": "insufficient",
    }
    assert "证据不完整" in render_gate_baseline(report)


def test_gate_baseline_signal_requires_both_benchmark_cis_to_exclude_zero() -> None:
    trades = [GateTrade(f"d:{idx}", "RISK_ON", 5.0 + idx * 0.1, 4.0 + idx * 0.1, -0.1) for idx in range(30)]
    assert _protocol(trades)["signal"] == "inconclusive"
    trades = [GateTrade(f"d:{idx}", "RISK_ON", 5.0 + idx * 0.1, 4.0 + idx * 0.1, 3.0 + idx * 0.1) for idx in range(30)]
    assert _protocol(trades)["signal"] == "positive"
    assert _protocol([])["signal"] == "inconclusive"


def test_contrast_status_requires_both_benchmarks_to_separate_in_the_same_direction() -> None:
    def pooled(wyckoff_main, wyckoff_small, rs_main, rs_small):
        return {
            "wyckoff": {"off": {"excess_main": {"ci95": wyckoff_main}, "excess_smallcap": {"ci95": wyckoff_small}}},
            "random": {"off": {"excess_main": {"ci95": rs_main}, "excess_smallcap": {"ci95": rs_small}}},
        }

    assert _contrast_status(pooled((-2, -1), (-2, -1), (0, 1), (0, 1)), "off", "random") == "wyckoff_below_random"
    assert _contrast_status(pooled((1, 2), (1, 2), (-1, 0), (-1, 0)), "off", "random") == "wyckoff_above_random"
    assert _contrast_status(pooled((-2, -1), (1, 2), (0, 1), (-1, 0)), "off", "random") == "indistinguishable"
    assert _contrast_status(pooled((-2, 0.5), (-2, 0.5), (0, 1), (0, 1)), "off", "random") == "indistinguishable"
    assert _contrast_status(pooled(None, (-2, -1), (0, 1), (0, 1)), "off", "random") == "insufficient"
