"""Tests for the capital-context / signal-health forward alpha evaluation."""

from __future__ import annotations

import pandas as pd

from scripts.evaluate_capital_context_alpha import MIN_GROUP, _contrast, _read_verdict, build_report


def _frame(rows: list[tuple[str, int, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"d": pd.Timestamp(day), "flag": flag, "return_pct": ret} for day, flag, ret in rows])


class TestContrast:
    def test_reports_insufficient_sample(self):
        result = _contrast(_frame([("2026-06-01", 1, 1.0), ("2026-06-01", 0, 0.0)]), "flag", "x")
        assert result["verdict"] == "样本不足"
        assert result["min_group"] == MIN_GROUP

    def test_pairs_within_day_only(self):
        """只含单组的交易日不能进入配对，否则市场水温会被当成信号。"""
        rows = [("2026-06-01", 1, 5.0)] * 40 + [("2026-06-01", 0, 1.0)] * 40
        rows += [("2026-06-02", 1, 9.0)] * 40  # 该日无对照组，应被排除
        result = _contrast(_frame(rows), "flag", "x")
        assert result["paired_days"] == 1

    def test_detects_positive_effect(self):
        rows = []
        for day in range(1, 11):
            date = f"2026-06-{day:02d}"
            rows += [(date, 1, 4.0)] * 5 + [(date, 0, -4.0)] * 5
        result = _contrast(_frame(rows), "flag", "x")
        assert result["paired_diff"] == 8.0
        assert result["paired_diff_inside_noise"] is False


class TestVerdictReading:
    def test_inside_noise_wins_over_sign(self):
        assert "无区分力" in _read_verdict(3.0, (1.0, 5.0), True)

    def test_ci_crossing_zero_blocks_wiring(self):
        assert "噪声边缘" in _read_verdict(1.0, (-2.0, 4.0), False)

    def test_negative_is_not_promotable(self):
        assert "不可用于晋级" in _read_verdict(-2.8, (-5.2, -0.3), False)

    def test_positive_and_clean(self):
        assert _read_verdict(2.0, (0.5, 3.5), False).startswith("正向")


class TestBuildReport:
    def _outcomes(self) -> pd.DataFrame:
        rows = []
        for day in range(1, 9):
            for idx in range(20):
                rows.append(
                    {
                        "horizon_days": 5,
                        "status": "done",
                        "trade_date": f"2026-06-{day:02d}",
                        "code": f"60000{idx % 5}",
                        "signal_type": "sos",
                        "regime": "NEUTRAL",
                        "return_pct": 1.0 if idx % 2 else -1.0,
                        "max_drawdown_pct": -3.0,
                    }
                )
        return pd.DataFrame(rows)

    def test_uses_only_prior_health(self):
        """当日 health 不得参与（allow_exact_matches=False），否则是未来信息。"""
        outcomes = self._outcomes()
        health = pd.DataFrame(
            [
                {
                    "horizon_days": 5,
                    "as_of_date": "2026-06-01",
                    "signal_type": "sos",
                    "regime": "NEUTRAL",
                    "health_state": "HEALTHY",
                    "sample_count": 40,
                    "avg_return_pct": 1.0,
                }
            ]
        )
        report = build_report(outcomes, health, pd.DataFrame(), horizon=5)
        matched = report["health"]["matched_outcomes"]
        # 6-01 当天的 20 条不能匹配上同日 health，只有之后的交易日可以。
        assert matched == len(outcomes) - 20

    def test_capital_coverage_reported_even_when_sparse(self):
        observations = pd.DataFrame(
            [
                {"code": "600000", "trade_date": "2026-06-02", "features_json": {"source_context": {"lhb": {}}}},
                {"code": "600001", "trade_date": "2026-06-02", "features_json": {}},
            ]
        )
        report = build_report(self._outcomes(), pd.DataFrame(), observations, horizon=5)
        capital = report["capital_context"]
        assert capital["with_capital"] == 1
        assert capital["coverage_pct"] == 50.0
        assert capital["contrast"]["verdict"] == "样本不足"

    def test_baseline_is_reported(self):
        report = build_report(self._outcomes(), pd.DataFrame(), pd.DataFrame(), horizon=5)
        assert report["matured_outcomes"] == 160
        assert report["baseline_ret"] == 0.0
