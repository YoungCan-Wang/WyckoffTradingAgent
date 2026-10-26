from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflows.backtest_regime_timing import (
    BenchBar,
    build_regime_timing,
    in_market,
    load_regime_timing_inputs,
    render_regime_timing,
    simulate_timing,
)

_BARS = tuple(BenchBar(f"2025-03-{day:02d}", 100.0 + day, 100.0 + day + 0.5) for day in range(1, 6))


def _write_cell(
    root: Path, period: str, regimes: dict[str, str], bars: tuple[BenchBar, ...], *, smallcap: bool
) -> None:
    directory = root / f"backtest-gate-{period}-off-wyckoff-7"
    directory.mkdir(parents=True)
    coverage = {"selection_coverage": [{"signal_date": date, "regime": regime} for date, regime in regimes.items()]}
    (directory / "selection_coverage_x.json").write_text(json.dumps(coverage), encoding="utf-8")
    csv_text = "date,open,close\n" + "".join(f"{b.date.replace('-', '')},{b.open},{b.close}\n" for b in bars)
    (directory / "benchmark_main.csv").write_text(csv_text, encoding="utf-8")
    if smallcap:
        (directory / "benchmark_smallcap.csv").write_text(csv_text, encoding="utf-8")


def test_simulate_charges_switches_and_uses_open_on_entry_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflows.backtest_regime_timing.slippage_bps_per_side", lambda: 0.0)
    monkeypatch.setattr("workflows.backtest_regime_timing.ETF_COMMISSION_PCT_PER_SIDE", 0.0)
    regimes = {"2025-03-01": "RISK_OFF", "2025-03-02": "NEUTRAL", "2025-03-03": "NEUTRAL", "2025-03-04": "CRASH"}
    hold = simulate_timing(_BARS, regimes, "buy_hold", frozenset())
    timed = simulate_timing(_BARS, regimes, "avoid_hard_defense", frozenset())

    assert hold["days"] == timed["days"] == 4
    # 所有变体都从空仓起步：持有基准也在首日开盘买入并计一次切换，与择时变体同口径。
    assert hold["time_in_market"] == 100.0 and hold["switches"] == 1
    assert hold["total_return"] == pytest.approx((105.5 / 102.0 - 1.0) * 100.0)
    assert timed["switches"] == 2 and timed["time_in_market"] == 50.0
    # 03-02 收盘判 NEUTRAL → 03-03 开盘 103 买入，03-04 收盘 104.5 判 CRASH → 03-05 开盘 105 卖出。
    assert timed["total_return"] == pytest.approx((105.0 / 103.0 - 1.0) * 100.0)
    assert timed["max_drawdown"] == 0.0


def test_in_market_variants_follow_regime_sets() -> None:
    blocked = frozenset({"RISK_ON", "NEUTRAL", "RISK_OFF"})
    assert in_market("buy_hold", "CRASH", blocked)
    assert not in_market("avoid_hard_defense", "RISK_OFF", blocked)
    assert in_market("avoid_hard_defense", "RISK_ON", blocked)
    assert in_market("risk_on_only", "RISK_ON", blocked) and not in_market("risk_on_only", "NEUTRAL", blocked)
    assert in_market("production_gate", "CAUTION", blocked) and not in_market("production_gate", "NEUTRAL", blocked)


def test_report_loads_first_cell_per_period_and_marks_missing_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "workflows.backtest_regime_timing.oms_buy_block_regimes", lambda: frozenset({"RISK_OFF", "NEUTRAL"})
    )
    regimes = {bar.date: ("RISK_OFF" if idx % 2 else "NEUTRAL") for idx, bar in enumerate(_BARS)}
    _write_cell(tmp_path, "bear_2022", regimes, _BARS, smallcap=True)
    _write_cell(tmp_path, "bull_2025", regimes, _BARS, smallcap=False)
    (tmp_path / "backtest-gate-bear_2022-live-rs-7").mkdir()

    inputs = load_regime_timing_inputs(tmp_path)
    report = build_regime_timing(inputs)

    assert set(inputs) == {"bear_2022", "bull_2025"}
    assert set(inputs["bull_2025"].bench) == {"main"}
    assert inputs["bear_2022"].bench["main"][0].date == "2025-03-01"
    assert report["status"] == "incomplete"
    assert report["missing_periods"] == ["bull_2020", "sideways_2023", "volatile_2024", "recent_6m"]
    assert report["production_gate_allowed"] == []
    assert {cell["variant"] for cell in report["cells"]} == {
        "buy_hold",
        "avoid_hard_defense",
        "risk_on_only",
        "production_gate",
    }
    assert all(verdict["status"] == "insufficient" for verdict in report["verdict"].values())
    assert {row["regime"] for row in report["conditional_forward"]["main"]} == {"RISK_OFF", "NEUTRAL"}
    markdown = render_regime_timing(report)
    assert "| bear_2022 | 上证 | buy_hold |" in markdown
    assert "`insufficient`" in markdown
