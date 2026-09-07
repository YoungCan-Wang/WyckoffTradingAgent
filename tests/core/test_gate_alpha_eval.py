"""Tests for gate-layer alpha evaluation (L3 theme resonance + stop-loss staleness).

判定口径 2026-09-07 改过：``excess`` 是没配平动量的裸差值，只作描述，任何方向性结论都
必须来自 ``swap``（逐对随机互换否证）。所以这里的测试分两类——没有对照时**必须拒绝**给
结论，有对照时结论必须来自对照而不是差值符号。
"""

from __future__ import annotations

import pytest

from core.funnel_effect_eval import SwapTest
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


def _swap(observed: float, p: float = 0.005, *, days: int = 100, column: str = "stock_win") -> SwapTest:
    """构造一个互换结果。``days`` 默认给足,否则 SwapTest 会先短路成「尚未可知」。"""
    return SwapTest(
        column=column,
        days=days,
        avg_pairs=225.5,
        observed_pct=observed,
        null_avg_pct=0.0,
        null_sd_pct=0.65,
        p_one_sided=p / 2,
        p_two_sided=p,
        permutations=200,
    )


class TestSummarize:
    def test_requires_minimum_days(self):
        stat = summarize("x", _daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.verdict == "样本不足"
        assert stat.excess is None

    def test_equal_weights_days_not_symbols(self):
        """每日等权：个股多的日子不该主导均值。"""
        stat = summarize("x", [{"inside": 10.0, "outside": 0.0, "size": 1000.0}, *_daily([(0.0, 0.0)] * 4)])
        assert stat.excess == pytest.approx(2.0)

    def test_skips_rows_with_missing_side(self):
        rows = _daily([(1.0, 0.0)] * 5) + [{"inside": None, "outside": 0.0, "size": 1.0}]
        assert summarize("x", rows).days == 5

    def test_marks_production_row(self):
        stat = summarize(f"topN={PROD_TOP_N_SECTORS}", _daily([(1.0, 0.0)] * 5), is_production=True)
        assert stat.is_production is True
        assert stat.as_dict()["is_production"] is True


class TestVerdictRequiresControl:
    """没有对照就不许出现方向性词汇——首轮就是这么把动量读成「贡献」的。"""

    def test_bare_difference_refuses_to_conclude(self):
        stat = summarize("x", _daily([(2.0, 0.0)] * 6))
        assert "不是结论" in stat.verdict
        assert "正贡献" not in stat.verdict
        assert "负贡献" not in stat.verdict

    def test_bare_difference_is_flagged_uncontrolled_in_json(self):
        assert summarize("x", _daily([(2.0, 0.0)] * 6)).as_dict()["excess_is_controlled"] is False

    @pytest.mark.parametrize("excess", [3.0, -3.0])
    def test_sign_of_excess_does_not_drive_verdict(self, excess: float):
        """同一个对照结果,裸差值取正或取负都不该改变判定。"""
        rows = _daily([(excess, 0.0)] * 6)
        stat = summarize("x", rows, swap_question="待测 vs 对照", swap={"stock_win": _swap(-1.7, 0.005)})
        assert "没有正向信息" in stat.verdict

    def test_controlled_verdict_comes_from_win_column(self):
        stat = summarize(
            "x",
            _daily([(-0.4, 0.0)] * 6),
            swap_question="非热门 vs 热门（动量配平）",
            swap={"stock_win": _swap(3.064, 0.005), "gross_return": _swap(0.488, 0.005, column="gross_return")},
        )
        assert "非热门 vs 热门（动量配平）" in stat.verdict
        assert "超出互换零分布" in stat.verdict
        assert "+0.488" in stat.verdict  # 收益栏必须一起报,不能只报胜率

    def test_null_control_says_no_information(self):
        stat = summarize("x", _daily([(-0.4, 0.0)] * 6), swap={"stock_win": _swap(0.041, 0.821)})
        assert "不含信息" in stat.verdict

    def test_short_control_is_not_a_failure(self):
        """天数不够是「尚未可知」,不能读成「没通过」。"""
        stat = summarize("x", _daily([(-0.4, 0.0)] * 6), swap={"stock_win": _swap(5.0, 0.005, days=15)})
        assert "尚未可知" in stat.verdict

    def test_missing_win_column_is_not_a_conclusion(self):
        stat = summarize("x", _daily([(-0.4, 0.0)] * 6), swap={"gross_return": _swap(1.0, column="gross_return")})
        assert stat.verdict == "对照未跑出结果：尚未可知"

    def test_short_sample_keeps_swap_question(self):
        """样本不足那条路径也要把待测方向带上,否则 JSON 里这行读不出量的是哪一侧。"""
        stat = summarize("x", _daily([(1.0, 0.0)] * (MIN_DAYS - 1)), swap_question="未触发 vs 已触发（动量配平）")
        assert stat.as_dict()["swap_question"] == "未触发 vs 已触发（动量配平）"

    def test_swap_is_serialized(self):
        stat = summarize("x", _daily([(-0.4, 0.0)] * 6), swap={"stock_win": _swap(3.064)})
        payload = stat.as_dict()["swap"]
        assert payload["stock_win"]["observed_pct"] == 3.064
        assert payload["stock_win"]["permutations"] == 200


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
    def _report(
        self,
        theme_excess: float,
        stop_excesses: list[float],
        *,
        theme_swap: dict[str, SwapTest] | None = None,
        stop_swap: dict[str, SwapTest] | None = None,
    ) -> GateReport:
        report = GateReport()
        report.theme = [
            GateStat(
                f"topN={PROD_TOP_N_SECTORS}",
                100,
                240,
                -0.95,
                -0.36,
                theme_excess,
                48.0,
                True,
                "非热门 vs 热门（动量配平）",
                theme_swap,
            ),
            GateStat("topN=20", 100, 1050, -0.31, -0.44, 0.14, 56.0, False),
        ]
        report.stop_loss = [
            GateStat(label, 100, 500, -0.75, -0.35, value, 40.0, False, "未触发 vs 已触发（动量配平）", stop_swap)
            for label, value in zip(["0~15%", "15~30%", "30~50%", ">50%"], stop_excesses, strict=False)
        ]
        return report

    def test_marks_production_row_in_table(self):
        assert "（生产值）" in render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))

    def test_uncontrolled_table_says_bare_difference(self):
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "裸差值" in text
        assert "市场（未匹配）" in text

    def test_no_control_refuses_both_directions(self):
        """没跑对照时既不能说止损正确,也不能说该放宽——两个方向都没有测量支撑。"""
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "不要" in text and "止损正确" in text
        assert "放宽风控" in text

    def test_null_stop_control_reports_no_support_either_way(self):
        text = render(self._report(-0.59, [-0.4] * 4, stop_swap={"stock_win": _swap(0.041, 0.821)}))
        assert "没有测量支撑" in text
        assert "保持现状是默认，不是结论" in text

    def test_passing_theme_still_demands_monthly_split(self):
        """过了置换检验也只是过了一道,必须把跨窗口反号这件事写在行动项里。"""
        text = render(self._report(-0.59, [-0.4] * 4, theme_swap={"stock_win": _swap(3.064, 0.005)}))
        assert "2 个月" in text
        assert "生产口径" in text

    def test_failing_theme_does_not_propose_widening_topn(self):
        text = render(self._report(-0.59, [-0.4] * 4, theme_swap={"stock_win": _swap(-1.739, 0.005)}))
        assert "别拿裸差值去调 topN" in text
        assert "放宽到" not in text

    def test_cost_threshold_is_always_stated(self):
        """任何改动建议都必须带上成本门槛，避免拿 0.1pct 的增益去改参数。"""
        assert "0.202%" in render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))

    def test_reading_guide_states_control_is_the_verdict(self):
        text = render(self._report(-0.59, [-0.4, -0.42, -0.22, -0.05]))
        assert "判定（配平动量后）" in text

    def test_p_value_optimism_is_disclosed(self):
        """持有期重叠 → 观测不独立,而置换零分布当它们独立,p 偏乐观,必须写出来。"""
        assert "偏乐观" in render(self._report(-0.59, [-0.4] * 4))


class TestReportSerialization:
    def test_json_serializable(self):
        import json

        report = GateReport()
        report.theme = [GateStat("topN=5", 100, 240, -0.95, -0.36, -0.59, 48.0, True)]
        report.stop_loss = [GateStat("0~15%", 100, 500, -0.75, -0.35, -0.40, 46.0)]
        payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
        assert payload["production"]["top_n_sectors"] == PROD_TOP_N_SECTORS
        assert payload["theme_resonance"][0]["excess"] == -0.59

    def test_swap_survives_json_round_trip(self):
        import json

        report = GateReport()
        report.theme = [
            GateStat("topN=5", 100, 240, -0.95, -0.36, -0.59, 48.0, True, "非热门 vs 热门", {"stock_win": _swap(3.064)})
        ]
        payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
        assert payload["theme_resonance"][0]["swap"]["stock_win"]["observed_pct"] == 3.064

    def test_empty_report_is_safe(self):
        assert GateReport().as_dict()["theme_resonance"] == []
