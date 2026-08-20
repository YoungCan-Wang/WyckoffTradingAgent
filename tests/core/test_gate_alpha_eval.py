"""Tests for gate-layer alpha evaluation (L3 theme resonance + stop-loss staleness)."""

from __future__ import annotations

import pytest

from core.gate_alpha_eval import (
    MIN_DAYS,
    PROD_TOP_N_SECTORS,
    GateReport,
    GateStat,
    band_of,
    render,
    summarize,
)


def _daily(pairs: list[tuple[float, float]], size: float = 10.0) -> list[dict[str, float]]:
    return [{"inside": a, "outside": b, "size": size} for a, b in pairs]


class TestSummarize:
    def test_requires_minimum_days(self):
        stat = summarize("x", _daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.verdict == "样本不足"
        assert stat.excess is None

    def test_equal_weights_days_not_symbols(self):
        """每日等权：个股多的日子不该主导均值。"""
        stat = summarize("x", [{"inside": 10.0, "outside": 0.0, "size": 1000.0}, *_daily([(0.0, 0.0)] * 4)])
        assert stat.excess == pytest.approx(2.0)

    def test_positive_and_negative_verdicts(self):
        pos = summarize("p", _daily([(2.0, 0.0), (3.0, 0.0), (4.0, 0.0), (5.0, 0.0), (6.0, 0.0)]))
        neg = summarize("n", _daily([(-2.0, 0.0), (-3.0, 0.0), (-4.0, 0.0), (-5.0, 0.0), (-6.0, 0.0)]))
        assert pos.verdict == "正贡献"
        assert neg.verdict == "负贡献"

    def test_near_random_is_flagged(self):
        """为正日占比落在 45~55% 时不给方向性结论，避免把噪声当信号。"""
        stat = summarize("r", _daily([(1.0, 0.0), (-1.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (2.0, 0.0), (-2.0, 0.0)]))
        assert stat.positive_day_pct == pytest.approx(50.0)
        assert "接近随机" in stat.verdict

    def test_skips_rows_with_missing_side(self):
        rows = _daily([(1.0, 0.0)] * 5) + [{"inside": None, "outside": 0.0, "size": 1.0}]
        assert summarize("x", rows).days == 5

    def test_marks_production_row(self):
        stat = summarize(f"topN={PROD_TOP_N_SECTORS}", _daily([(1.0, 0.0)] * 5), is_production=True)
        assert stat.is_production is True
        assert stat.as_dict()["is_production"] is True


class TestBandOf:
    def test_maps_each_band(self):
        assert band_of(5.0) == "0~15%"
        assert band_of(20.0) == "15~30%"
        assert band_of(40.0) == "30~50%"
        assert band_of(120.0) == ">50%"

    def test_boundaries_are_left_inclusive(self):
        assert band_of(15.0) == "15~30%"
        assert band_of(30.0) == "30~50%"
        assert band_of(50.0) == ">50%"

    def test_negative_deviation_is_not_stale(self):
        """参考价低于现价不属于陈旧问题，应被排除而非归入 0~15%。"""
        assert band_of(-1.0) is None


class TestRender:
    def _report(self, theme_excess: float, stop_excesses: list[float]) -> GateReport:
        report = GateReport()
        report.theme = [
            GateStat(f"topN={PROD_TOP_N_SECTORS}", 100, 240, -0.95, -0.36, theme_excess, 48.0, True),
            GateStat("topN=20", 100, 1050, -0.31, -0.44, 0.14, 56.0, False),
        ]
        report.stop_loss = [
            GateStat(label, 100, 500, -0.75, -0.35, value, 40.0)
            for label, value in zip(["0~15%", "15~30%", "30~50%", ">50%"], stop_excesses, strict=False)
        ]
        return report

    def test_marks_production_row_in_table(self):
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "（生产值）" in text

    def test_all_negative_stop_bands_warn_against_loosening(self):
        """四档全负时必须明确说别因个别反例放宽风控。"""
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "不要因为个别陈旧参考价的反例去放宽风控" in text

    def test_positive_stop_band_is_named(self):
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, 0.05]))
        assert ">50%" in text
        assert "卖早了" in text

    def test_theme_negative_reports_best_alternative(self):
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "反向筛选" in text
        assert "topN=20" in text

    def test_cost_threshold_is_always_stated(self):
        """任何改动建议都必须带上成本门槛，避免拿 0.1pct 的增益去改参数。"""
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "0.202%" in text

    def test_reading_guide_explains_sign_convention(self):
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        # 止损档的符号约定与题材层相反，必须写清楚。
        assert "超额为负 = 止损正确" in text


class TestReportSerialization:
    def test_json_serializable(self):
        import json

        report = GateReport()
        report.theme = [GateStat("topN=5", 100, 240, -0.95, -0.36, -0.59, 48.0, True)]
        report.stop_loss = [GateStat("0~15%", 100, 500, -0.75, -0.35, -0.40, 46.0)]
        payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
        assert payload["production"]["top_n_sectors"] == PROD_TOP_N_SECTORS
        assert payload["theme_resonance"][0]["excess"] == -0.59

    def test_empty_report_is_safe(self):
        assert GateReport().as_dict()["theme_resonance"] == []
