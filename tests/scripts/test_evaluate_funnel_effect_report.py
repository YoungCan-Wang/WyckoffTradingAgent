"""漏斗效果报告的结论段：三种否证理由不能混成一句。

这里守的是一个真实读错：修好抵消基准后 T+10 配对超额 +2.468pct **高过**种子上界
+2.095,但差距 1.414 小于种子宽度 1.555。旧版对这一格印「落在随机负控制区间内」,
读报告的人会以为超额被区间包住了。计数也曾按 verdict 里的「落在」字面来分,句式一改
计数就静默变错——现在按 ``beats_band`` 分,本文件盯着这条不许退回去。
"""

from __future__ import annotations

from scripts import evaluate_funnel_effect as mod


def _absolute(net: float = -3.82) -> dict:
    return {
        "days": 30,
        "avg_size": 17.2,
        "net_pct": net,
        "net_t": -1.97,
        "positive_day_pct": 43.3,
        "worst_day_pct": -29.94,
        "best_day_pct": 18.05,
        "bench_days": 0,
        "bench_pct": None,
        "bench_excess_pct": None,
        "bench_excess_t": None,
        "verdict": "绝对收益为负：这批票拿着是亏的",
    }


def _matched(excess: float) -> dict:
    return {
        "label": "matched",
        "days": 30,
        "avg_size": 15.1,
        "net_pct": -5.44,
        "control_pct": -9.56,
        "excess_pct": excess,
        "excess_t": 1.48,
        "positive_day_pct": 60.0,
        "residual_mom_pct": 0.02,
    }


def _block(excess: float, controls: tuple[float, ...]) -> dict:
    """单个持有期的结果块,control_gap 走真实实现而不是手搓字典。"""
    from core.funnel_effect_eval import GroupStat, control_gap

    def stat(e: float) -> GroupStat:
        return GroupStat("x", 30, 15.1, -5.44, -9.56, e, 1.48, 60.0, 0.02)

    return {
        "absolute": _absolute(),
        "matched": _matched(excess),
        "controls": [],
        "control_gap": control_gap(stat(excess), [stat(c) for c in controls]),
    }


def _render(excess: float, controls: tuple[float, ...]) -> str:
    result = {"status": "formal_l4", "horizons": {"10": _block(excess, controls)}}
    return mod.render(result, "formal_l4")


class TestSummaryCountsByBand:
    def test_above_the_band_but_thin_is_not_called_inside(self) -> None:
        """实测 T+10 这一格：+2.468 高过上界 +2.095,不能说「落在区间内」。"""
        out = _render(2.468, (0.540, 1.054, 2.095))
        assert "幅度小于种子自身的抽样宽度" in out
        assert "没有高过随机负控制的上界" not in out
        assert "含独立选股信息；仍需" not in out

    def test_inside_the_band_counts_as_no_outperformance(self) -> None:
        out = _render(1.500, (0.540, 1.054, 2.095))
        assert "没有高过随机负控制的上界" in out
        assert "幅度小于种子自身的抽样宽度" not in out

    def test_below_every_seed_counts_the_same_as_inside(self) -> None:
        """比每个种子都差,和被区间包住一样都没跑赢,归同一段结论。"""
        out = _render(0.100, (0.540, 1.054, 2.095))
        assert "没有高过随机负控制的上界" in out
        assert "随便挑还更好" in out

    def test_clear_win_gets_the_win_paragraph_only(self) -> None:
        out = _render(6.000, (0.540, 1.054, 2.095))
        assert "含独立选股信息；仍需" in out
        assert "没有高过随机负控制的上界" not in out
        assert "幅度小于种子自身的抽样宽度" not in out

    def test_insufficient_sample_is_not_counted_as_a_win(self) -> None:
        """样本不足的格子既不算跑赢也不算否证,不能让它触发「含独立选股信息」。

        旧版结论段是「没被否证就宣布有效」（``not inside and not thin``）,全部持有期
        样本不足时两个计数都是 0,于是零证据印出「含独立选股信息」。
        """
        result = {"status": "formal_l4", "horizons": {"20": _block(2.0, (1.0,))}}
        out = mod.render(result, "formal_l4")
        assert result["horizons"]["20"]["control_gap"]["verdict"] == "样本不足"
        assert "含独立选股信息；仍需" not in out
        assert "所有持有期都样本不足" in out

    def test_a_thin_cell_does_not_suppress_a_real_win_elsewhere(self) -> None:
        """一格薄一格真赢时,别把两段都印出来——薄那格已经说明不能下结论。"""
        result = {
            "status": "formal_l4",
            "horizons": {
                "5": _block(2.468, (0.540, 1.054, 2.095)),
                "10": _block(6.000, (0.540, 1.054, 2.095)),
            },
        }
        out = mod.render(result, "formal_l4")
        assert "幅度小于种子自身的抽样宽度" in out
        assert "含独立选股信息；仍需" not in out
