"""Tests for pattern forward-return evaluation.

这个工具的职责是在放宽通道门槛**之前**先证伪：某类票抓到了到底赚不赚钱。
2026-08-22 首轮九档全为负超额（-0.71 ~ -0.98pct）且随阈值收紧单调恶化，
故未据此改动任何门槛。用例守住「不过成本门槛就不该行动」这条判定。
"""

from __future__ import annotations

import pytest

from core.pattern_forward_eval import (
    MIN_DAYS,
    MIN_HITS_PER_DAY,
    ROUND_TRIP_COST_PCT,
    HorizonResult,
    PatternReport,
    PatternSpec,
    summarize_horizon,
)


def _daily(pairs, hits: float = 10.0):
    return [{"net": n, "market": m, "hits": hits} for n, m in pairs]


class TestSpec:
    def test_defaults_match_first_run(self):
        spec = PatternSpec()
        assert spec.prev_day_max_pct == pytest.approx(3.0)
        assert spec.open_gap_max_pct == pytest.approx(4.0)
        assert spec.day_return_min_pct == pytest.approx(7.0)
        assert spec.min_avg_amount_wan == pytest.approx(8000.0)

    def test_describe_is_readable(self):
        text = PatternSpec(open_gap_max_pct=3.0, day_return_min_pct=8.0).describe()
        assert "T开盘<=3%" in text
        assert "T涨幅>8%" in text


class TestSummarize:
    def test_requires_minimum_days(self):
        result = summarize_horizon(5, _daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert result.verdict == "样本不足"
        assert result.net_excess_pct is None

    def test_drops_days_with_too_few_hits(self):
        """命中不足 3 只的日子不计入——个别票会主导当日均值。"""
        rows = _daily([(1.0, 0.0)] * MIN_DAYS, hits=MIN_HITS_PER_DAY - 1)
        assert summarize_horizon(5, rows).verdict == "样本不足"

    def test_equal_weights_days_not_symbols(self):
        rows = [{"net": 10.0, "market": 0.0, "hits": 500.0}, *_daily([(0.0, 0.0)] * (MIN_DAYS - 1))]
        assert summarize_horizon(5, rows).net_excess_pct == pytest.approx(10.0 / MIN_DAYS)

    def test_negative_verdict(self):
        result = summarize_horizon(5, _daily([(-1.0, 0.5)] * MIN_DAYS))
        assert result.verdict == "负贡献"
        assert result.net_excess_pct == pytest.approx(-1.5)

    def test_near_random_has_no_direction(self):
        pairs = [(1.0, 0.0), (-1.0, 0.0)] * (MIN_DAYS // 2)
        result = summarize_horizon(5, _daily(pairs))
        assert result.positive_day_pct == pytest.approx(50.0)
        assert result.verdict == "无方向性"


class TestCostThreshold:
    def test_small_positive_not_actionable(self):
        """+0.1pct 撑不过 0.202% 往返成本，不足以支持放宽门槛。"""
        assert HorizonResult(5, 200, 50.0, 0.1, 0.0, 70.0).actionable is False

    def test_large_positive_actionable(self):
        assert HorizonResult(5, 200, 50.0, 0.9, 0.0, 70.0).actionable is True

    def test_none_excess_not_actionable(self):
        assert HorizonResult(5, 0, 0.0, None, None, None).actionable is False

    def test_first_run_values_are_not_actionable(self):
        """首轮实测 -0.71/-0.82 必须判为不可行动。"""
        for net, mkt in ((-0.27, 0.44), (-0.38, 0.44), (-0.14, 0.71)):
            assert HorizonResult(5, 276, 84.0, net, mkt, 39.0).actionable is False


class TestReport:
    def test_any_actionable_false_when_all_negative(self):
        report = PatternReport(spec=PatternSpec())
        report.results = [
            HorizonResult(5, 276, 84.0, -0.27, 0.44, 39.0),
            HorizonResult(10, 276, 84.0, -0.14, 0.71, 43.0),
        ]
        assert report.any_actionable is False
        assert report.at(10) is not None
        assert report.at(99) is None

    def test_serializable(self):
        import json

        report = PatternReport(spec=PatternSpec())
        report.results = [HorizonResult(5, 276, 84.0, -0.27, 0.44, 39.0)]
        payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
        assert payload["any_actionable"] is False
        assert payload["round_trip_cost_pct"] == pytest.approx(ROUND_TRIP_COST_PCT)
        assert payload["results"][0]["verdict"] == "负贡献"
