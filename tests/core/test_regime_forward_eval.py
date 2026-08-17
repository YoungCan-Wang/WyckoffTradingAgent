"""Tests for market-regime forward evaluation."""

from __future__ import annotations

from core.regime_forward_eval import (
    MIN_REGIME_DAYS,
    evaluate_regimes,
    forward_return_map,
    trailing_drop_map,
)


def _series(n: int, step: float = 1.0, start: float = 100.0):
    dates = [f"2026-06-{i + 1:02d}" for i in range(n)]
    closes = [start + i * step for i in range(n)]
    return dates, closes


class TestForwardReturnMap:
    def test_drops_tail_without_full_horizon(self):
        dates, closes = _series(10)
        forward = forward_return_map(dates, closes, 5)
        assert len(forward) == 5
        assert dates[-1] not in forward

    def test_computes_percent(self):
        forward = forward_return_map(["d1", "d2", "d3"], [100.0, 110.0, 121.0], 1)
        assert round(forward["d1"], 4) == 10.0
        assert round(forward["d2"], 4) == 10.0

    def test_skips_non_positive_base(self):
        assert forward_return_map(["a", "b"], [0.0, 5.0], 1) == {}


class TestTrailingDropMap:
    def test_uses_lookback_window(self):
        drops = trailing_drop_map(["a", "b", "c", "d"], [100.0, 90.0, 80.0, 70.0], lookback=3)
        assert round(drops["d"], 2) == -30.0
        assert "a" not in drops


class TestEvaluateRegimes:
    def test_reports_insufficient_sample(self):
        dates, closes = _series(40)
        regimes = {d: "CRASH" for d in dates[:3]}
        report = evaluate_regimes(regimes, dates, closes, horizon=5)
        crash = next(s for s in report.stats if s.regime == "CRASH")
        assert crash.days < MIN_REGIME_DAYS
        assert crash.verdict == "样本不足"
        assert crash.p_value is None

    def test_detects_positive_regime(self):
        """把 regime 打在每次下跌后的低点，前瞻应显著为正。"""
        dates = [f"2026-06-{i + 1:02d}" for i in range(40)]
        closes = []
        price = 100.0
        for i in range(40):
            price = price * (0.95 if i % 4 == 0 else 1.03)
            closes.append(price)
        regimes = {dates[i]: ("CRASH" if i % 4 == 0 else "NEUTRAL") for i in range(34)}
        report = evaluate_regimes(regimes, dates, closes, horizon=3)
        crash = next(s for s in report.stats if s.regime == "CRASH")
        assert crash.days >= MIN_REGIME_DAYS
        assert crash.excess > 0

    def test_p_value_is_directional(self):
        """负超额也要拿到小 p；固定单尾会让负向结果得到 p≈0.99。"""
        dates = [f"2026-06-{i + 1:02d}" for i in range(40)]
        closes = []
        price = 100.0
        for i in range(40):
            price = price * (1.05 if i % 4 == 0 else 0.99)
            closes.append(price)
        # 打在每次上涨之后（即后续偏跌处）
        regimes = {dates[i]: ("RISK_ON" if i % 4 == 0 else "NEUTRAL") for i in range(34)}
        report = evaluate_regimes(regimes, dates, closes, horizon=3)
        risk_on = next(s for s in report.stats if s.regime == "RISK_ON")
        assert risk_on.excess < 0
        assert risk_on.p_value is not None and risk_on.p_value <= 0.5

    def test_baseline_uses_all_days(self):
        dates, closes = _series(30)
        report = evaluate_regimes({dates[0]: "NEUTRAL"}, dates, closes, horizon=5)
        assert report.baseline_days == len(dates) - 5
        assert report.baseline is not None

    def test_empty_series_is_safe(self):
        report = evaluate_regimes({}, [], [], horizon=5)
        assert report.baseline is None
        assert report.stats == []

    def test_stats_sorted_best_first(self):
        dates = [f"2026-06-{i + 1:02d}" for i in range(40)]
        closes = []
        price = 100.0
        for i in range(40):
            price = price * (0.95 if i % 4 == 0 else 1.03)
            closes.append(price)
        regimes = {dates[i]: ("CRASH" if i % 4 == 0 else "NEUTRAL") for i in range(34)}
        report = evaluate_regimes(regimes, dates, closes, horizon=3)
        excesses = [s.excess for s in report.stats if s.excess is not None]
        assert excesses == sorted(excesses, reverse=True)


class TestDrawdownControl:
    def test_flags_pure_drawdown_equivalence(self):
        """CRASH 就是「跌最多」时，应判为可能只是均值回复。"""
        dates = [f"2026-06-{i + 1:02d}" for i in range(40)]
        closes = []
        price = 100.0
        for i in range(40):
            price = price * (0.95 if i % 4 == 0 else 1.03)
            closes.append(price)
        regimes = {dates[i]: ("CRASH" if i % 4 == 0 else "NEUTRAL") for i in range(34)}
        report = evaluate_regimes(regimes, dates, closes, horizon=3)
        control = report.drawdown_control
        assert control["crash_days"] >= MIN_REGIME_DAYS
        assert "overlap_days" in control
