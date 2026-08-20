"""Tests for Wyckoff purity evaluation.

守两类结论，防止以后被误读：
- 原版覆盖率只是事实陈述，不是缺陷指标（实测补全为负收益）。
- alpha 随持有期衰减必须被识别出来，否则会有人试图靠拉长持有期放大它。
"""

from __future__ import annotations

import pytest

from core.wyckoff_purity_eval import (
    HORIZONS,
    MIN_DAYS,
    ROUND_TRIP_COST_PCT,
    WYCKOFF_CANON,
    EventCurve,
    HorizonStat,
    canon_coverage,
    summarize_horizon,
)


def _daily(pairs: list[tuple[float, float]], hits: float = 20.0) -> list[dict[str, float]]:
    return [{"event": a, "market": b, "hits": hits} for a, b in pairs]


def _stat(horizon: int, excess: float, positive: float = 60.0, days: int = 200) -> HorizonStat:
    return HorizonStat(horizon, days, 30.0, excess, 0.0, excess, positive)


class TestSummarizeHorizon:
    def test_requires_minimum_days(self):
        stat = summarize_horizon(5, _daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.verdict == "样本不足"
        assert stat.excess is None

    def test_equal_weights_days_not_symbols(self):
        """按交易日等权：命中个股多的日子不该主导均值。"""
        rows = [{"event": 10.0, "market": 0.0, "hits": 5000.0}, *_daily([(0.0, 0.0)] * (MIN_DAYS - 1))]
        assert summarize_horizon(5, rows).excess == pytest.approx(10.0 / MIN_DAYS)

    def test_near_random_has_no_direction(self):
        pairs = [(1.0, 0.0), (-1.0, 0.0)] * (MIN_DAYS // 2)
        stat = summarize_horizon(5, _daily(pairs))
        assert stat.positive_day_pct == pytest.approx(50.0)
        assert stat.verdict == "无方向性"

    def test_negative_verdict(self):
        stat = summarize_horizon(5, _daily([(-1.0, 0.0)] * MIN_DAYS))
        assert stat.verdict == "负贡献"

    def test_skips_rows_missing_a_side(self):
        rows = _daily([(1.0, 0.0)] * MIN_DAYS) + [{"event": None, "market": 0.0, "hits": 1.0}]
        assert summarize_horizon(5, rows).days == MIN_DAYS


class TestCostThreshold:
    def test_small_positive_does_not_beat_cost(self):
        """+0.1pct 的超额撑不过 0.202% 往返成本，不该被当成可落地的改动依据。"""
        assert _stat(5, 0.1).beats_cost is False

    def test_large_positive_beats_cost(self):
        assert _stat(5, 0.43).beats_cost is True

    def test_none_excess_does_not_beat_cost(self):
        assert HorizonStat(5, 0, 0.0, None, None, None, None).beats_cost is False


class TestEventCurve:
    def test_detects_decay(self):
        """短周期为正、长周期转负 → 判定为衰减。SOS 实测就是这个形态。"""
        curve = EventCurve("SOS", True, [_stat(5, 0.36), _stat(10, -0.10), _stat(20, -0.57), _stat(40, -0.89)])
        assert curve.decays is True

    def test_persistent_negative_is_not_decay(self):
        """Spring 各周期全负，属持续为负而非衰减，两者含义不同。"""
        curve = EventCurve("Spring", True, [_stat(h, -0.5) for h in HORIZONS])
        assert curve.decays is False

    def test_best_picks_highest_excess(self):
        curve = EventCurve("x", True, [_stat(5, 0.1), _stat(10, 0.43), _stat(20, -0.2), _stat(40, -0.5)])
        assert curve.best.horizon == 10

    def test_best_is_none_without_usable_stats(self):
        curve = EventCurve("x", True, [HorizonStat(h, 0, 0.0, None, None, None, None) for h in HORIZONS])
        assert curve.best is None
        assert curve.decays is False

    def test_serializable(self):
        import json

        curve = EventCurve("SOS", True, [_stat(5, 0.36), _stat(40, -0.89)])
        payload = json.loads(json.dumps(curve.as_dict(), ensure_ascii=False))
        assert payload["decays_with_horizon"] is True
        assert payload["stats"][0]["beats_cost"] is True


class TestCanonCoverage:
    def test_reports_missing_phase_a_events(self):
        """原版吸筹阶段 A 的四个事件（PS/SC/AR/ST）本仓均未实现。"""
        coverage = canon_coverage()
        missing = " ".join(coverage["missing"])
        for code in ("PS", "SC", "AR", "ST"):
            assert code in missing

    def test_implemented_are_the_late_stage_events(self):
        coverage = canon_coverage()
        assert set(coverage["implemented"]) == {"Spring", "LPS", "SOS"}

    def test_coverage_pct_matches_canon(self):
        coverage = canon_coverage()
        expected = round(100.0 * sum(1 for _, _, done in WYCKOFF_CANON if done) / len(WYCKOFF_CANON), 1)
        assert coverage["coverage_pct"] == expected

    def test_cost_constant_is_aligned_with_friction_model(self):
        """成本门槛须与 core.trade_friction 的实测往返成本一致。"""
        from core.trade_friction import round_trip_cost_pct

        assert ROUND_TRIP_COST_PCT == pytest.approx(round_trip_cost_pct(), abs=0.01)
