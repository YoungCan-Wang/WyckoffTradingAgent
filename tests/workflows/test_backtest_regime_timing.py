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
    sentiment_states,
    simulate_timing,
)

_BARS = tuple(BenchBar(f"2025-03-{day:02d}", 100.0 + day, 100.0 + day + 0.5) for day in range(1, 6))
_SENTIMENT_HEADER = "date,symbols,advancers,decliners,up_limit,down_limit,touched_up,broke_up,max_streak\n"


def _labels(regimes: dict[str, str], sentiment: str = "warm") -> dict[str, dict[str, str]]:
    return {day: {"regime": regime, "sentiment": sentiment} for day, regime in regimes.items()}


def _write_cell(
    root: Path, period: str, regimes: dict[str, str], bars: tuple[BenchBar, ...], *, smallcap: bool
) -> None:
    directory = root / f"backtest-gate-{period}-off-wyckoff-7"
    directory.mkdir(parents=True)
    coverage = {"selection_coverage": [{"signal_date": date, "regime": regime} for date, regime in regimes.items()]}
    (directory / "selection_coverage_x.json").write_text(json.dumps(coverage), encoding="utf-8")
    csv_text = "date,open,close\n" + "".join(f"{b.date.replace('-', '')},{b.open},{b.close}\n" for b in bars)
    (directory / "benchmark_main.csv").write_text(csv_text, encoding="utf-8")
    sentiment = _SENTIMENT_HEADER + "".join(f"{b.date},10,5,5,{idx},0,{idx},0,1\n" for idx, b in enumerate(bars))
    (directory / "market_sentiment_x.csv").write_text(sentiment, encoding="utf-8")
    if smallcap:
        (directory / "benchmark_smallcap.csv").write_text(csv_text, encoding="utf-8")


def test_simulate_charges_switches_and_uses_open_on_entry_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflows.backtest_regime_timing.slippage_bps_per_side", lambda: 0.0)
    monkeypatch.setattr("workflows.backtest_regime_timing.ETF_COMMISSION_PCT_PER_SIDE", 0.0)
    regimes = {"2025-03-01": "RISK_OFF", "2025-03-02": "NEUTRAL", "2025-03-03": "NEUTRAL", "2025-03-04": "CRASH"}
    hold = simulate_timing(_BARS, _labels(regimes), "buy_hold", frozenset())
    timed = simulate_timing(_BARS, _labels(regimes), "avoid_hard_defense", frozenset())

    assert hold["days"] == timed["days"] == 4
    # 所有变体都从空仓起步：持有基准也在首日开盘买入并计一次切换，与择时变体同口径。
    assert hold["time_in_market"] == 100.0 and hold["switches"] == 1
    assert hold["total_return"] == pytest.approx((105.5 / 102.0 - 1.0) * 100.0)
    assert timed["switches"] == 2 and timed["time_in_market"] == 50.0
    # 03-02 收盘判 NEUTRAL → 03-03 开盘 103 买入，03-04 收盘 104.5 判 CRASH → 03-05 开盘 105 卖出。
    assert timed["total_return"] == pytest.approx((105.0 / 103.0 - 1.0) * 100.0)
    assert timed["max_drawdown"] == 0.0


def test_in_market_variants_follow_regime_and_sentiment_sets() -> None:
    blocked = frozenset({"RISK_ON", "NEUTRAL", "RISK_OFF"})

    def label(regime: str, sentiment: str = "ebb") -> dict[str, str]:
        return {"regime": regime, "sentiment": sentiment}

    assert in_market("buy_hold", label("CRASH"), blocked)
    assert not in_market("avoid_hard_defense", label("RISK_OFF"), blocked)
    assert in_market("avoid_hard_defense", label("RISK_ON"), blocked)
    assert in_market("risk_on_only", label("RISK_ON"), blocked)
    assert not in_market("risk_on_only", label("NEUTRAL"), blocked)
    assert in_market("production_gate", label("CAUTION"), blocked)
    assert not in_market("production_gate", label("NEUTRAL"), blocked)
    assert in_market("sentiment_follow", label("CRASH", "climax"), blocked)
    assert not in_market("sentiment_follow", label("RISK_ON", "ebb"), blocked)
    assert in_market("sentiment_not_ice", label("CRASH", "ebb"), blocked)
    assert not in_market("sentiment_not_ice", label("RISK_ON", "ice"), blocked)
    assert in_market("sentiment_ice_contrarian", label("CRASH", "ice"), blocked)
    assert not in_market("sentiment_ice_contrarian", label("CRASH", "warm"), blocked)


def test_sentiment_states_use_trailing_percentile_after_min_obs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflows.backtest_regime_timing.SENTIMENT_WINDOW", 5)
    monkeypatch.setattr("workflows.backtest_regime_timing.SENTIMENT_MIN_OBS", 3)
    days = [f"d{i}" for i in range(7)]
    states = sentiment_states(days, [0, 10, 20, 30, 40, -5, 25])
    assert "d0" not in states and "d1" not in states
    assert states["d2"] == "warm"  # 窗口 [0,10,20] 里严格小于 20 的有 2 个 → 分位 0.67
    assert states["d4"] == "climax"  # 窗口 [0,10,20,30,40] 里小于 40 的有 4 个 → 分位 0.8
    assert states["d5"] == "ice"  # -5 是窗口 [10,20,30,40,-5] 的最小值 → 分位 0
    assert states["d6"] == "ebb"  # 窗口 [20,30,40,-5,25] 里小于 25 的有 2 个 → 分位 0.4


def test_report_loads_first_cell_per_period_and_marks_missing_periods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "workflows.backtest_regime_timing.oms_buy_block_regimes", lambda: frozenset({"RISK_OFF", "NEUTRAL"})
    )
    monkeypatch.setattr("workflows.backtest_regime_timing.SENTIMENT_MIN_OBS", 2)
    regimes = {bar.date: ("RISK_OFF" if idx % 2 else "NEUTRAL") for idx, bar in enumerate(_BARS)}
    _write_cell(tmp_path, "bear_2022", regimes, _BARS, smallcap=True)
    _write_cell(tmp_path, "bull_2025", regimes, _BARS, smallcap=False)
    (tmp_path / "backtest-gate-bear_2022-live-rs-7").mkdir()

    inputs = load_regime_timing_inputs(tmp_path)
    report = build_regime_timing(inputs)

    assert set(inputs) == {"bear_2022", "bull_2025"}
    assert set(inputs["bull_2025"].bench) == {"main"}
    assert inputs["bear_2022"].bench["main"][0].date == "2025-03-01"
    # 首日情绪窗口不足 → 不进可比日集合；净涨停数逐日递增，分位 0.5/0.67/0.75 为 warm，到 0.8 才是 climax。
    assert set(inputs["bear_2022"].labels) == {bar.date for bar in _BARS[1:]}
    assert [label["sentiment"] for _, label in sorted(inputs["bear_2022"].labels.items())] == ["warm"] * 3 + ["climax"]
    assert report["status"] == "incomplete"
    assert report["missing_periods"] == ["bull_2020", "sideways_2023", "volatile_2024", "recent_6m"]
    assert report["production_gate_allowed"] == []
    assert {cell["variant"] for cell in report["cells"]} == {
        "buy_hold",
        "avoid_hard_defense",
        "risk_on_only",
        "production_gate",
        "sentiment_follow",
        "sentiment_not_ice",
        "sentiment_ice_contrarian",
    }
    assert all(verdict["status"] == "insufficient" for verdict in report["verdict"].values())
    assert {row["label"] for row in report["conditional_forward"]["regime"]["main"]} == {"RISK_OFF", "NEUTRAL"}
    assert {row["label"] for row in report["conditional_forward"]["sentiment"]["main"]} == {
        "warm"
    }  # 末日 climax 无次日收益
    markdown = render_regime_timing(report)
    assert "| bear_2022 | 上证 | buy_hold |" in markdown
    assert "## 情绪状态（净涨停数 250 日分位） → 次日指数收益" in markdown
    assert "`insufficient`" in markdown
