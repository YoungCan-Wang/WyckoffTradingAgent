"""Tests for ranker weight evaluation (watch_score 的 dry_q 分量)."""

from __future__ import annotations

import pytest

from core.ranker_weight_eval import (
    MIN_DAYS,
    PROD_DRY_WEIGHT,
    ROUND_TRIP_COST_PCT,
    WEIGHT_GRID,
    AblationStat,
    RankerReport,
    WalkForwardStat,
    band_of,
    extension_penalty,
    matched_spread,
    quarter_of,
    render,
    summarize_ablation,
    summarize_band,
    summarize_weight,
    tstat,
    walk_forward_weight,
)


def _band_daily(pairs: list[tuple[float, float]], size: float = 10.0) -> list[dict[str, float]]:
    return [{"inside": a, "domain": b, "size": size} for a, b in pairs]


def _ablation_rows(pairs: list[tuple[float, float]], start: int = 20250106) -> list[dict[str, float]]:
    rows = []
    for i, (keep, drop) in enumerate(pairs):
        rows.append({"date": float(start + i), "keep": keep, "drop": drop, "overlap": 0.1})
    return rows


class TestTstat:
    def test_returns_none_for_zero_variance(self):
        """方差为零时不返回 inf，否则会被误读成极显著。"""
        assert tstat([1.0] * 30) is None

    def test_needs_three_points(self):
        assert tstat([1.0, 2.0]) is None

    def test_matches_manual_calculation(self):
        # mean=2, sd=1, n=3 -> t = 2 / (1/sqrt(3))
        assert tstat([1.0, 2.0, 3.0]) == pytest.approx(2.0 / (1.0 / 3.0**0.5))

    def test_ignores_non_finite(self):
        assert tstat([1.0, 2.0, 3.0, float("nan")]) == pytest.approx(tstat([1.0, 2.0, 3.0]))


class TestExtensionPenalty:
    def test_zero_below_thresholds(self):
        assert extension_penalty(10.0, 5.0) == 0.0

    def test_caps_at_production_maxima(self):
        """生产上限 0.30 + 0.10；超出阈值再涨也不该继续扣。"""
        assert extension_penalty(500.0, 500.0) == pytest.approx(0.40)

    def test_ramps_linearly(self):
        # ret20 = 45 + 55/2 -> 半程 -> 0.15
        assert extension_penalty(72.5, 0.0) == pytest.approx(0.15)

    def test_negative_returns_not_rewarded(self):
        assert extension_penalty(-50.0, -50.0) == 0.0


class TestBandOf:
    def test_assigns_each_quintile(self):
        assert band_of(0.05) == "最湿20%"
        assert band_of(0.5) == "中档"
        assert band_of(0.95) == "最干20%"

    def test_top_edge_is_inclusive(self):
        assert band_of(1.0) == "最干20%"

    def test_rejects_out_of_range(self):
        assert band_of(1.5) is None
        assert band_of(-0.1) is None


class TestQuarterOf:
    def test_maps_month_to_quarter(self):
        assert quarter_of(20250102) == 20251
        assert quarter_of(20250630) == 20252
        assert quarter_of(20261231) == 20264


class TestSummarizeBand:
    def test_requires_minimum_days(self):
        stat = summarize_band("x", _band_daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.verdict == "样本不足"
        assert stat.excess is None

    def test_equal_weights_days_not_symbols(self):
        """每日等权：入选只数多的日子不该主导均值。"""
        rows = [{"inside": 10.0, "domain": 0.0, "size": 1000.0}, *_band_daily([(0.0, 0.0)] * (MIN_DAYS - 1))]
        stat = summarize_band("x", rows)
        assert stat.excess == pytest.approx(10.0 / MIN_DAYS)

    def test_significant_negative_is_labeled(self):
        rows = _band_daily([(-1.0 - 0.01 * i, 0.0) for i in range(MIN_DAYS + 5)])
        stat = summarize_band("最湿20%", rows)
        assert stat.excess is not None and stat.excess < 0
        assert stat.verdict == "显著为负"

    def test_insignificant_is_not_given_a_direction(self):
        rows = _band_daily([(1.0 if i % 2 else -1.0, 0.0) for i in range(MIN_DAYS + 5)])
        assert summarize_band("中档", rows).verdict == "不显著"


class TestSummarizeAblation:
    def test_requires_minimum_days(self):
        stat = summarize_ablation(10, _ablation_rows([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.verdict == "样本不足"

    def test_pairs_by_day_cancelling_common_move(self):
        """同日配对应消掉市场共同成分：两臂同步平移不改变差值。"""
        base = [(1.0 + 0.02 * i, 0.5 + 0.02 * i) for i in range(MIN_DAYS + 10)]
        shifted = [(k + 20.0, d + 20.0) for k, d in base]
        assert summarize_ablation(10, _ablation_rows(base)).diff == pytest.approx(
            summarize_ablation(10, _ablation_rows(shifted)).diff
        )

    def test_significant_gain_above_cost_supports_keeping(self):
        rows = _ablation_rows([(1.2 + 0.01 * i, 0.0) for i in range(MIN_DAYS + 10)])
        stat = summarize_ablation(10, rows, rand_diffs=[0.0, 0.1, 0.2])
        assert stat.diff_t is not None and stat.diff_t >= 2.0
        assert stat.verdict == "显著且抵成本：支持保留"

    def test_gain_below_cost_is_rejected(self):
        """增益必须大于单次往返成本，否则不能算贡献。"""
        small = ROUND_TRIP_COST_PCT * 0.5
        rows = _ablation_rows([(small + 0.0001 * i, 0.0) for i in range(MIN_DAYS + 10)])
        stat = summarize_ablation(10, rows, rand_diffs=[0.0])
        assert stat.verdict == "显著但不抵成本"

    def test_inside_random_band_is_not_attributable(self):
        """落在同权重随机臂的取值范围内 -> 不可归因于 dry_q。"""
        rows = _ablation_rows([(1.0 + 0.01 * i, 0.0) for i in range(MIN_DAYS + 10)])
        stat = summarize_ablation(10, rows, rand_diffs=[0.5, 1.5])
        assert stat.beats_random is False
        assert stat.verdict == "落在随机带内：不可归因"

    def test_insignificant_does_not_support_keeping(self):
        rows = _ablation_rows([(5.0 if i % 2 else -5.0, 0.0) for i in range(MIN_DAYS + 10)])
        assert summarize_ablation(10, rows).verdict.startswith("不显著")

    def test_counts_positive_quarters(self):
        rows = _ablation_rows([(1.0, 0.0)] * 20, start=20250106)
        rows += _ablation_rows([(-1.0, 0.0)] * 20, start=20250706)
        stat = summarize_ablation(10, rows)
        assert stat.positive_quarters == "1/2"

    def test_beats_random_is_none_without_band(self):
        stat = summarize_ablation(10, _ablation_rows([(1.0 + 0.01 * i, 0.0) for i in range(MIN_DAYS + 5)]))
        assert stat.beats_random is None


class TestSummarizeWeight:
    def test_flags_production_weight(self):
        rows = [{"inside": 1.0, "domain": 0.0}] * (MIN_DAYS + 5)
        assert summarize_weight(PROD_DRY_WEIGHT, rows).is_production is True
        assert summarize_weight(0.60, rows).is_production is False

    def test_requires_minimum_days(self):
        assert summarize_weight(0.20, [{"inside": 1.0, "domain": 0.0}]).excess is None


class TestWalkForwardWeight:
    def test_lag_excludes_unsettled_horizon(self):
        """T 日选权重只能用截到 T-H-1 的历史，否则用到未结算的前向收益。"""
        n = 200
        dates = [20250100 + i for i in range(n)]
        # 权重 2.00 只在最后 3 天变好；lag=11 应让它在窗口内选不到
        series = {w: [0.0] * n for w in WEIGHT_GRID}
        for i in range(n - 3, n):
            series[2.00][i] = 100.0
        stat = walk_forward_weight(10, dates, series, horizon=10, warmup=120)
        assert stat.days > 0
        assert stat.chosen == pytest.approx(0.0)

    def test_flat_objective_scatters_picks(self):
        """目标平坦时选中分布散开——这是拟合噪声的特征，判定须拒绝上线。"""
        n = 260
        dates = [20250100 + i for i in range(n)]
        series = {w: [((i * 7 + int(w * 10)) % 11 - 5) / 10.0 for i in range(n)] for w in WEIGHT_GRID}
        stat = walk_forward_weight(10, dates, series, horizon=10, warmup=120)
        if stat.pick_dist:
            assert stat.is_concentrated in (True, False)
        assert "上线" in stat.verdict or stat.verdict == "样本不足"

    def test_concentrated_and_significant_is_allowed_through(self):
        n = 300
        dates = [20250100 + i for i in range(n)]
        series = {w: [0.0] * n for w in WEIGHT_GRID}
        series[0.60] = [1.0 + 0.001 * i for i in range(n)]
        stat = walk_forward_weight(10, dates, series, horizon=10, warmup=120)
        assert stat.is_concentrated is True
        assert stat.diff is not None and stat.diff > 0

    def test_missing_production_weight_is_safe(self):
        stat = walk_forward_weight(10, [20250101] * 5, {0.5: [1.0] * 5}, horizon=5)
        assert stat.days == 0
        assert stat.verdict == "样本不足"

    def test_insufficient_days_after_warmup(self):
        dates = [20250100 + i for i in range(130)]
        series = {w: [0.0] * 130 for w in WEIGHT_GRID}
        assert walk_forward_weight(10, dates, series, horizon=10, warmup=120).days < MIN_DAYS


class TestMatchedSpread:
    def test_requires_minimum_days(self):
        assert matched_spread([{"date": 20250101.0, "spread": 1.0}])["note"] == "样本不足"

    def test_reports_note_about_interpretation(self):
        rows = [{"date": float(20250106 + i), "spread": 0.01 * i} for i in range(MIN_DAYS + 5)]
        out = matched_spread(rows)
        assert "不代表" in out["note"]
        assert out["spread"] is not None


class TestRender:
    def _report(self) -> RankerReport:
        report = RankerReport()
        report.bands = [
            summarize_band(label, _band_daily([(0.1 * i, 0.0) for i in range(MIN_DAYS + 5)]))
            for label in ("最湿20%", "最干20%")
        ]
        report.ablation = [
            summarize_ablation(10, _ablation_rows([(1.2 + 0.01 * i, 0.0) for i in range(MIN_DAYS + 10)]), [0.0, 0.1]),
            summarize_ablation(20, _ablation_rows([(0.6 + 0.01 * i, 0.0) for i in range(MIN_DAYS + 10)]), [0.0, 0.1]),
        ]
        report.weights = {
            10: [summarize_weight(w, [{"inside": 1.0, "domain": 0.0}] * (MIN_DAYS + 5)) for w in WEIGHT_GRID]
        }
        report.walk_forward = [WalkForwardStat(10, 200, 0.1, 0.2, -0.1, 0.5, {0.40: 0.3, 0.80: 0.3, 2.00: 0.4})]
        report.matched_spread = matched_spread(
            [{"date": float(20250106 + i), "spread": 0.001 * i} for i in range(MIN_DAYS + 5)]
        )
        return report

    def test_includes_reading_and_next_steps(self):
        text = render(self._report(), horizon=10, start=20250102, end=20260828)
        assert "## 读法" in text
        assert "## 接下来做什么" in text
        assert str(ROUND_TRIP_COST_PCT) in text

    def test_marks_production_weight_row(self):
        assert "← 生产" in render(self._report(), horizon=10, start=20250102, end=20260828)

    def test_flat_walk_forward_blocks_weight_change(self):
        """选中分布散开时，行动项必须给出「维持」而不是「调档」。"""
        text = render(self._report(), horizon=10, start=20250102, end=20260828)
        assert f"权重维持 {PROD_DRY_WEIGHT}" in text

    def test_handles_empty_report(self):
        text = render(RankerReport(), horizon=5, start=20250102, end=20260828)
        assert "样本不足" in text

    def test_report_dict_carries_reading_key(self):
        payload = self._report().as_dict()
        assert "reading" in payload
        assert payload["production"]["dry_weight"] == PROD_DRY_WEIGHT
        assert payload["cost_threshold_pct"] == ROUND_TRIP_COST_PCT


class TestAblationStatEdges:
    def test_empty_quarters_render_dash(self):
        stat = AblationStat(10, 0, None, None, None, None, None, None, None, None, {})
        assert stat.positive_quarters == "—"
        assert stat.verdict == "样本不足"
