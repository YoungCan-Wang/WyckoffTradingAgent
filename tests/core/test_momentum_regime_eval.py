"""Tests for momentum regime evaluation (RPS gate value + dynamic-switch designs)."""

from __future__ import annotations

import json
import math
import random

import pytest

from core.momentum_regime_eval import (
    CONTROL_SEEDS,
    MIN_DAYS,
    NON_TOP_CAP,
    PROD_RPS_FAST_MIN,
    PROD_RPS_SLOW_MIN,
    BandStat,
    MomentumReport,
    SwitchStat,
    control_gap,
    ic_persistence,
    quarter_of,
    render,
    summarize_band,
    tstat,
    walk_forward_switch,
)


def _daily(pairs: list[tuple[float, float]], size: float = 40.0) -> list[dict[str, float]]:
    """逐日观测。日期落在 2025 年 1 月,故都归入同一季度。"""
    return [
        {"date": 20250100 + i + 1, "inside": inside, "domain": domain, "size": size}
        for i, (inside, domain) in enumerate(pairs)
    ]


def _switch_rows(
    states: list[float],
    *,
    gates: list[float] | None = None,
    mids: list[float] | None = None,
    start: int = 20250100,
) -> list[dict[str, float]]:
    n = len(states)
    gates = gates if gates is not None else [1.0] * n
    mids = mids if mids is not None else [0.0] * n
    return [
        {"date": start + i + 1, "gate": g, "mid": m, "state": s}
        for i, (s, g, m) in enumerate(zip(states, gates, mids, strict=True))
    ]


def _overlapping_ic(horizon: int, n: int = 600, seed: int = 7) -> list[float]:
    """把独立噪声按 H 日滚动平均,人造出「相邻窗口共用 H-1 项」的重叠结构。"""
    rng = random.Random(seed)
    noise = [rng.gauss(0.0, 1.0) for _ in range(n + horizon)]
    return [sum(noise[i : i + horizon]) / horizon for i in range(n)]


class TestTstat:
    def test_known_series(self):
        # 均值 3、样本标准差 sqrt(2.5)、n=5 -> 3 / (sqrt(2.5/5)) = 4.2426
        assert tstat([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(4.2426, abs=1e-3)

    def test_zero_variance_is_none(self):
        """常数序列的 t 值无定义,不能返回 inf 混进报告。"""
        assert tstat([2.0, 2.0, 2.0, 2.0]) is None

    def test_too_few_points_is_none(self):
        assert tstat([1.0, 2.0]) is None

    def test_ignores_nan_and_none(self):
        assert tstat([1.0, 2.0, 3.0, 4.0, 5.0, float("nan"), None]) == pytest.approx(4.2426, abs=1e-3)

    def test_sign_follows_mean(self):
        assert tstat([-1.0, -2.0, -3.0, -4.0, -5.0]) < 0


class TestQuarterOf:
    def test_maps_each_quarter(self):
        assert quarter_of(20260215) == 20261
        assert quarter_of(20260515) == 20262
        assert quarter_of(20260815) == 20263
        assert quarter_of(20261115) == 20264

    def test_quarter_boundaries(self):
        assert quarter_of(20260331) == 20261
        assert quarter_of(20260401) == 20262
        assert quarter_of(20261231) == 20264


class TestSummarizeBand:
    def test_requires_minimum_days(self):
        """样本不足时返回占位而非抛错——构造参数变化过一次,这条守着它。"""
        stat = summarize_band("x", _daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.verdict == "样本不足"
        assert stat.excess is None
        assert stat.inside_t is None

    def test_equal_weights_days_not_symbols(self):
        """每日等权:入选只数多的日子不该主导均值。"""
        rows = [
            {"date": 20250101, "inside": 10.0, "domain": 0.0, "size": 900.0},
            *_daily([(0.0, 0.0)] * (MIN_DAYS - 1), size=1.0),
        ]
        assert summarize_band("x", rows).excess == pytest.approx(10.0 / MIN_DAYS)

    def test_computes_both_t_stats(self):
        rows = _daily([(2.0, 1.0)] * MIN_DAYS)
        rows[0]["inside"] = 8.0
        stat = summarize_band("x", rows)
        assert stat.inside_t is not None
        assert stat.excess_t is not None
        assert stat.inside_ret == pytest.approx(stat.domain_ret + stat.excess)

    def test_insignificant_excess_is_called_zero_expectation(self):
        """|t| < 2 时不给方向性结论,避免把噪声读成选择价值。"""
        pairs = [(1.0, 0.0), (-1.0, 0.0)] * (MIN_DAYS // 2)
        stat = summarize_band("x", pairs and _daily(pairs))
        assert stat.excess == pytest.approx(0.0)
        assert stat.verdict == "期望为零：无选择价值"

    def test_significant_signs(self):
        pos = summarize_band("p", _daily([(3.0, 0.0), (3.1, 0.0)] * (MIN_DAYS // 2)))
        neg = summarize_band("n", _daily([(-3.0, 0.0), (-3.1, 0.0)] * (MIN_DAYS // 2)))
        assert pos.verdict == "正贡献"
        assert neg.verdict == "负贡献"

    def test_skips_rows_with_missing_side(self):
        rows = _daily([(1.0, 0.0)] * MIN_DAYS) + [{"date": 20250199, "inside": None, "domain": 0.0, "size": 1.0}]
        assert summarize_band("x", rows).days == MIN_DAYS

    def test_groups_excess_by_quarter(self):
        rows = _daily([(2.0, 0.0)] * MIN_DAYS) + [
            {"date": 20250400 + i + 1, "inside": -2.0, "domain": 0.0, "size": 40.0} for i in range(MIN_DAYS)
        ]
        stat = summarize_band("x", rows)
        assert stat.by_quarter[20251] == pytest.approx(2.0)
        assert stat.by_quarter[20252] == pytest.approx(-2.0)

    def test_marks_production_row(self):
        stat = summarize_band("65/70", _daily([(1.0, 0.0)] * MIN_DAYS), is_production=True)
        assert stat.is_production is True
        assert stat.as_dict()["is_production"] is True


class TestWalkForwardSwitch:
    def test_requires_more_than_warmup(self):
        stat = walk_forward_switch("x", _switch_rows([1.0] * 30), state_key="state", warmup=20)
        assert stat.verdict == "样本不足"
        assert stat.diff is None

    def test_warmup_days_are_not_scored(self):
        """预热期只用来定阈值,不能进入收益统计。"""
        rows = _switch_rows([float(i) for i in range(100)])
        stat = walk_forward_switch("x", rows, state_key="state", warmup=40)
        assert stat.days == 60

    def test_uses_no_future_information(self):
        """改掉未来的 state 不该影响历史上任何一天的开关决定。"""
        states = [float(i % 17) for i in range(200)]
        base = walk_forward_switch("x", _switch_rows(states), state_key="state", warmup=60)
        tampered = list(states)
        tampered[-30:] = [999.0] * 30
        rows = _switch_rows(tampered)
        # 只保留与原序列同长的前段,后 30 天的收益也一并截掉,便于逐日比较。
        trimmed = walk_forward_switch("x", rows[:-30], state_key="state", warmup=60)
        untampered = walk_forward_switch("x", _switch_rows(states)[:-30], state_key="state", warmup=60)
        assert trimmed.as_dict() == untampered.as_dict()
        assert base.days == 140

    def test_high_is_on_direction(self):
        """state 单调上升时,high_is_on 应几乎全程开启;取反则几乎全程关闭。"""
        states = [float(i) for i in range(200)]
        rows = _switch_rows(states)
        on = walk_forward_switch("on", rows, state_key="state", warmup=60, high_is_on=True)
        off = walk_forward_switch("off", rows, state_key="state", warmup=60, high_is_on=False)
        assert on.on_rate == pytest.approx(1.0)
        assert off.on_rate == pytest.approx(0.0)

    def test_closed_state_falls_back_to_mid_not_cash(self):
        """关闭时退到中动量档。漏斗每天都要出票,对照不能是空仓。"""
        n = 200
        rows = _switch_rows([0.0] * n, gates=[1.0] * n, mids=[0.5] * n)
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60, high_is_on=True)
        assert stat.on_rate == pytest.approx(0.0)
        assert stat.switched_ret == pytest.approx(0.5)
        assert stat.baseline_ret == pytest.approx(1.0)

    def test_insignificant_diff_cannot_ship(self):
        """走前差值 t 不足 2 就不可上线——首轮四种设计全部倒在这里。"""
        rng = random.Random(11)
        n = 260
        rows = _switch_rows(
            [rng.gauss(0.0, 1.0) for _ in range(n)],
            gates=[rng.gauss(0.0, 2.0) for _ in range(n)],
            mids=[rng.gauss(0.0, 2.0) for _ in range(n)],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60)
        assert stat.diff_t is not None
        assert abs(stat.diff_t) < 2.0
        assert stat.verdict == "走前不显著：不可上线"

    def test_significant_diff_alone_is_not_enough(self):
        """全程关闭时 diff 一定显著,但那是纯机械收益,不该判为「走前显著」。

        实测：基线是固定放行闸门,而中动量档全期高出闸门 0.671pct,同关闭率的**随机**
        开关表就能拿到 t=+3.70~+5.17。所以 diff_t 过线不构成上线理由。
        """
        n = 200
        # state 恒定 -> current > threshold 为假 -> 全程关闭 -> 全部取 mid,稳定高于 gate。
        rows = _switch_rows(
            [1.0] * n,
            gates=[0.0] * n,
            mids=[0.9 if i % 2 else 1.1 for i in range(n)],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60)
        assert stat.diff == pytest.approx(1.0, abs=0.02)
        assert stat.diff_t is not None and stat.diff_t > 2.0
        # 全程关闭 -> 没有开启日可比 -> 价差无法检验 -> 不可判定,而非「显著」。
        assert stat.spread_t is None
        assert stat.verdict == "机械项未分解：不可判定"
        assert stat.mechanical == pytest.approx(1.0, abs=0.02)
        assert stat.timing == pytest.approx(0.0, abs=0.02)

    def test_mechanical_gain_without_timing_is_called_out(self):
        """开关表与 (mid-gate) 无关时,择时项应归零、价差 t 不显著。"""
        n = 400
        rng = random.Random(5)
        # state 独立于价差 -> 开关表不含水温信息;mid 稳定高于 gate -> diff 仍显著。
        rows = _switch_rows(
            [rng.gauss(0.0, 1.0) for _ in range(n)],
            gates=[0.0] * n,
            mids=[1.0 + rng.gauss(0.0, 1.0) for _ in range(n)],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60)
        assert stat.diff_t is not None and stat.diff_t > 2.0
        assert stat.spread_t is not None and abs(stat.spread_t) < 2.0
        assert stat.timing == pytest.approx(0.0, abs=0.15)
        assert stat.verdict == "机械项主导：换档收益而非水温信息"

    def test_real_timing_survives_the_decomposition(self):
        """开关表真的挑中「价差大」的日子时,价差 t 显著、择时项为正。

        这是「识别水温」的理想情形:把价差做成 state 低时更大,走前中位数切换就会在
        低 state 关闭,正好落在价差大的日子上。两侧都掺噪声,否则方差为零、Welch t 无解。
        """
        n = 400
        rng = random.Random(3)
        states = [float(i % 40) for i in range(n)]
        rows = _switch_rows(
            states,
            gates=[0.0] * n,
            mids=[(2.0 if s < 20 else 0.1) + rng.gauss(0.0, 0.3) for s in states],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60, high_is_on=True)
        assert stat.spread_t is not None and stat.spread_t > 2.0
        assert stat.spread_off is not None and stat.spread_on is not None
        assert stat.spread_off > stat.spread_on
        assert stat.timing is not None and stat.timing > 0
        assert stat.verdict == "走前显著：值得进一步验证"

    def test_spread_t_uses_non_overlapping_days_only(self):
        """价差 t 必须按 H+1 步长抽样,否则重叠窗口会把它夸大。

        与 ic_persistence 同一个理由:相邻交易日的 H 日前向收益共用 H-1 天,
        直接拿全部日子算两样本 t 等于把样本量当成了 H 倍。
        """
        n = 600
        rng = random.Random(9)
        states = [float(i % 40) for i in range(n)]
        rows = _switch_rows(
            states,
            gates=[0.0] * n,
            mids=[(2.0 if s < 20 else 0.1) + rng.gauss(0.0, 0.3) for s in states],
        )
        daily = walk_forward_switch("x", rows, state_key="state", warmup=60, horizon=1)
        thinned = walk_forward_switch("x", rows, state_key="state", warmup=60, horizon=10)
        # 步长 H+1:H=10 用 1/11 的天数,H=1 用 1/2。
        assert daily.spread_days == pytest.approx((n - 60) // 2, abs=1)
        assert thinned.spread_days == pytest.approx((n - 60) // 11, abs=1)
        # 天数变少 -> t 变小,但均值不该变:均值仍用全部日子。
        assert abs(thinned.spread_t) < abs(daily.spread_t)
        assert thinned.spread_off == pytest.approx(daily.spread_off)
        assert thinned.spread_on == pytest.approx(daily.spread_on)
        # 机械项也用全部日子,不受抽样影响。
        assert thinned.mechanical == pytest.approx(daily.mechanical)
        assert thinned.diff == pytest.approx(daily.diff)

    def test_spread_t_reports_the_phase_range(self):
        """扫遍 H+1 个相位,报中位数并带出区间。单相位的 t 自己就是噪声。"""
        n = 600
        rng = random.Random(9)
        states = [float(i % 40) for i in range(n)]
        rows = _switch_rows(
            states,
            gates=[0.0] * n,
            mids=[(2.0 if s < 20 else 0.1) + rng.gauss(0.0, 0.3) for s in states],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60, horizon=10)
        assert stat.spread_t_min is not None and stat.spread_t_max is not None
        assert stat.spread_t_min <= stat.spread_t <= stat.spread_t_max
        # 效应够强时,区间下界也该过线——这正是「证据稳」的定义。
        assert stat.spread_t_min > 2.0
        assert stat.verdict == "走前显著：值得进一步验证"

    def test_phase_fragile_effect_does_not_pass(self):
        """效应弱到只有部分相位过线时,判定必须落在「证据不稳」。"""
        n = 600
        rng = random.Random(4)
        states = [float(i % 40) for i in range(n)]
        # 噪声放大到与信号同量级 -> 相位之间 t 拉开 -> 下界掉到 2 以下。
        rows = _switch_rows(
            states,
            gates=[0.0] * n,
            mids=[(1.2 if s < 20 else 0.1) + rng.gauss(0.0, 1.6) for s in states],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60, horizon=10)
        assert stat.spread_t_min is not None and stat.spread_t_min < 2.0
        assert stat.verdict != "走前显著：值得进一步验证"

    def test_mechanical_plus_timing_equals_diff(self):
        """分解必须是恒等式,否则表里三列会互相矛盾。"""
        n = 400
        rng = random.Random(3)
        states = [float(i % 40) for i in range(n)]
        rows = _switch_rows(
            states,
            gates=[0.0] * n,
            mids=[(2.0 if s < 20 else 0.1) + rng.gauss(0.0, 0.3) for s in states],
        )
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60)
        assert stat.mechanical is not None and stat.timing is not None
        assert stat.mechanical + stat.timing == pytest.approx(stat.diff)

    def test_on_rate_by_quarter_is_reported(self):
        """坏季度的开启率是否真的更低,是判断切换设计能不能识别水温的关键。"""
        rows = _switch_rows([float(i) for i in range(80)], start=20250100)
        rows += _switch_rows([0.0] * 80, start=20250400)
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60)
        assert stat.on_rate_by_quarter[20251] == pytest.approx(1.0)
        assert stat.on_rate_by_quarter[20252] == pytest.approx(0.0)


class TestControlGap:
    """随机负控制:中动量档的边缘到底是选择信息,还是只是「避开了顶部」。

    第二轮实测中动量档 +0.197,而只避开顶部的随机抽样 5 个种子给出
    +0.164~+0.210——落在区间内。缺了这个对照就会把 t=5.86 读成选择价值。
    """

    def _stat(self, excess: float | None) -> BandStat:
        return BandStat("x", 1079, 255.0, None, None, excess, 5.5)

    def test_mid_inside_control_range_has_no_selection_value(self):
        gap = control_gap(self._stat(0.197), [self._stat(e) for e in (0.210, 0.164, 0.199)])
        assert "落在" in gap["verdict"]
        assert gap["control_excess_min"] == pytest.approx(0.164)
        assert gap["control_excess_max"] == pytest.approx(0.210)

    def test_gap_within_seed_spread_is_not_a_win(self):
        """哪怕中动量档在所有种子之上,只要差距不超过种子间宽度就算不上跑赢。"""
        controls = [self._stat(e) for e in (0.10, 0.30)]
        gap = control_gap(self._stat(0.35), controls)
        assert gap["gap"] == pytest.approx(0.15)
        assert gap["seed_spread"] == pytest.approx(0.20)
        assert "落在" in gap["verdict"]

    def test_clear_outperformance_is_flagged_for_walk_forward(self):
        controls = [self._stat(e) for e in (0.10, 0.12)]
        gap = control_gap(self._stat(1.00), controls)
        assert "跑赢" in gap["verdict"]

    def test_single_seed_is_insufficient(self):
        """单种子的一次抽样量不出边缘宽度,不能拿来判定。"""
        assert control_gap(self._stat(0.197), [self._stat(0.164)])["verdict"] == "样本不足"

    def test_missing_mid_is_insufficient(self):
        assert control_gap(None, [self._stat(0.1), self._stat(0.2)])["verdict"] == "样本不足"
        assert control_gap(self._stat(None), [self._stat(0.1), self._stat(0.2)])["verdict"] == "样本不足"


class TestIcPersistence:
    def test_non_overlapping_rho_is_lower_on_overlapping_series(self):
        """重叠窗口会凭空造出高 lag-1 rho,非重叠口径必须把它拆掉。"""
        payload = ic_persistence(_overlapping_ic(horizon=5), horizon=5)
        naive = payload["lag1_overlapping"]
        honest = payload["lag1_non_overlapping"]
        assert naive > 0.6
        assert abs(honest) < 0.2
        assert honest < naive

    def test_step_is_horizon_plus_one(self):
        payload = ic_persistence([float(i) for i in range(100)], horizon=9)
        assert payload["segments"] == 10

    def test_note_marks_overlapping_as_artifact(self):
        payload = ic_persistence(_overlapping_ic(horizon=5), horizon=5)
        assert "假象" in payload["note"] or "不可用于判断持续性" in payload["note"]

    def test_handles_nan_and_short_input(self):
        payload = ic_persistence([0.1, float("nan"), 0.2], horizon=5)
        assert payload["lag1_non_overlapping"] is None
        assert payload["sign_persistence"] is None


def _band(
    label: str,
    *,
    inside: float,
    inside_t: float,
    excess: float,
    excess_t: float,
    is_production: bool = False,
) -> BandStat:
    return BandStat(
        label=label,
        days=402,
        avg_size=90.0,
        inside_ret=inside,
        domain_ret=inside - excess,
        excess=excess,
        excess_t=excess_t,
        inside_t=inside_t,
        by_quarter={20253: 1.66, 20254: 1.76, 20261: -0.37, 20262: 2.32, 20263: -6.63},
        is_production=is_production,
    )


def _report(
    *,
    prod_inside_t: float = 0.96,
    domain_inside_t: float = 1.27,
    switch_t: float = 0.27,
    spread_t: float = 0.30,
    spread_t_min: float = 0.08,
    spread_t_max: float = 1.92,
    mid_excess: float = 0.197,
    control_excesses: tuple[float, ...] = (0.210, 0.183, 0.164, 0.199, 0.174),
) -> MomentumReport:
    report = MomentumReport()
    report.thresholds = [
        _band("75/80", inside=0.51, inside_t=0.88, excess=0.15, excess_t=0.55),
        _band(
            f"{PROD_RPS_FAST_MIN:.0f}/{PROD_RPS_SLOW_MIN:.0f}",
            inside=0.432,
            inside_t=prod_inside_t,
            excess=0.070,
            excess_t=0.28,
            is_production=True,
        ),
    ]
    report.mid_band = _band("中动量档", inside=0.559, inside_t=1.10, excess=mid_excess, excess_t=5.86)
    report.domain = _band("流动性域内基准", inside=0.362, inside_t=domain_inside_t, excess=0.0, excess_t=0.0)
    report.domain.by_quarter = {}
    report.controls = [
        _band(
            f"随机负控制 <{NON_TOP_CAP:.0f} 分位 seed={seed}",
            inside=0.362 + excess,
            inside_t=1.20,
            excess=excess,
            excess_t=5.5,
        )
        for seed, excess in zip(CONTROL_SEEDS, control_excesses, strict=True)
    ]
    report.switches = [
        SwitchStat(
            "按市场宽度切换",
            282,
            0.472,
            0.432,
            0.040,
            switch_t,
            0.38,
            {20262: 0.35, 20263: 0.43},
            # 机械项占掉大部分差值,是实测形态:1080 天上 diff +0.543 里 +0.345 是机械的。
            mechanical=0.025,
            spread_off=0.85,
            spread_on=0.60,
            spread_t=spread_t,
            spread_t_min=spread_t_min,
            spread_t_max=spread_t_max,
            # 实测形态:960 天按 H+1=11 步长抽样,每相位 88 天。
            spread_days=88,
        ),
    ]
    report.ic_persistence = ic_persistence(_overlapping_ic(horizon=5), horizon=5)
    return report


class TestRender:
    def test_marks_production_row(self):
        assert "（生产值）" in render(_report(), 10)

    def test_shows_absolute_t_column(self):
        """判断闸门去留靠绝对 t 值,它必须出现在表里。"""
        text = render(_report(), 10)
        assert "绝对t" in text

    def test_higher_mean_but_lower_t_is_not_a_win(self):
        """闸门均值高于域内基准但 t 值更低时,不能给出「支持保留」。"""
        text = render(_report(prod_inside_t=0.96, domain_inside_t=1.27), 10)
        assert "**t 值更低**" in text
        assert "本轮支持保留" not in text

    def test_gain_below_cost_is_called_out(self):
        """t 值更高但增益不足成本时,净收益上看不出差别。"""
        text = render(_report(prod_inside_t=1.50, domain_inside_t=1.27), 10)
        assert "小于单次往返成本" in text
        assert "本轮支持保留" not in text

    def test_insignificant_excess_warns_against_tuning_thresholds(self):
        text = render(_report(), 10)
        assert "不要为了改善均值去调阈值" in text

    def test_control_rows_are_in_the_table(self):
        text = render(_report(), 10)
        assert "随机负控制" in text
        assert f"<{NON_TOP_CAP:.0f} 分位" in text

    def test_mid_matching_control_is_not_proposed_as_a_tier(self):
        """默认数据就是第二轮实测:中动量档落在控制区间内,不可当档位提案。"""
        text = render(_report(), 10)
        assert "只来自避开顶部" in text
        assert "顶部要躲开" in text
        assert "不是「越低越好」" in text

    def test_mid_beating_control_asks_for_walk_forward(self):
        text = render(_report(mid_excess=1.20), 10)
        assert "需按 walk_forward_switch 的走前口径复核" in text
        assert "只来自避开顶部" not in text

    def test_no_switch_survives_means_keep_fixed(self):
        text = render(_report(switch_t=0.27), 10)
        assert "维持固定放行" in text
        assert "样本内回看显著不算数" in text

    def test_significant_diff_without_spread_is_called_mechanical(self):
        """差值显著但价差不显著,必须说清赚的是换档、不是水温——否则会被当成上线依据。

        1080 天实测就是这个形态:按市场宽度切换 diff_t=+6.03,但同关闭率的随机开关表
        给 t=+3.70~+5.17,差值 t 过线本身没有区分力。
        """
        text = render(_report(switch_t=2.40, spread_t=0.30), 10)
        assert "按市场宽度切换" in text
        assert "赚的是「中动量档比闸门好」这个换档收益" in text
        assert "同关闭率的随机开关表拿得到同样的钱" in text
        assert "走前显著" not in text

    def test_surviving_switch_is_named(self):
        """差值与价差都过线才算幸存,此时才报出择时项。"""
        text = render(_report(switch_t=2.40, spread_t=3.57, spread_t_min=2.30), 10)
        assert "走前显著" in text
        assert "按市场宽度切换" in text
        assert "择时项" in text

    def test_phase_dependent_spread_is_flagged_as_unstable(self):
        """中位数过线但相位区间下界不到 2,必须说清换个相位结论就翻。

        1080 天实测:breadth 的 11 个相位给 +0.08~+1.92,0/11 到 2。中位数单独看
        会掩盖这一点。
        """
        text = render(_report(switch_t=2.40, spread_t=2.10, spread_t_min=0.08, spread_t_max=2.60), 10)
        assert "相位区间下界不到 2" in text
        assert "换个不重叠抽样相位结论就翻" in text
        assert "走前显著" not in text

    def test_spread_t_cell_shows_range_and_day_count(self):
        """表格里 t 必须带相位区间和不重叠天数,否则读者会当成 days 那么多样本。"""
        text = render(_report(switch_t=2.40, spread_t=1.38, spread_t_min=0.08, spread_t_max=1.92), 10)
        assert "+1.38 [+0.08, +1.92]" in text
        assert "（88天）" in text

    def test_undecidable_switch_is_not_read_as_passing(self):
        """没有对照日时差值全是机械项,不能落进「均不显著」那句话里蒙混过去。"""
        report = _report(switch_t=2.40)
        report.switches[0].spread_t = None
        report.switches[0].spread_on = None
        text = render(report, 10)
        assert "没有对照日" in text
        assert "不能读成走前通过" in text
        assert "均不显著" not in text

    def test_switch_table_warns_that_diff_t_is_not_a_test(self):
        """表下的注必须常驻,否则读者会拿差值 t 当结论。"""
        text = render(_report(), 10)
        assert "任何同关闭率的**随机**开关表" in text
        assert "价差t" in text

    def test_cost_threshold_and_walk_forward_are_always_stated(self):
        """任何改动建议都必须带上成本门槛和走前复验要求。"""
        text = render(_report(), 10)
        assert "0.202%" in text
        assert "walk_forward_switch" in text

    def test_ic_line_labels_overlapping_as_artifact(self):
        text = render(_report(), 10)
        assert "非重叠窗口 lag-1 rho" in text
        assert "是假象" in text

    def test_quarter_columns_come_from_thresholds(self):
        text = render(_report(), 10)
        for quarter in (20253, 20263):
            assert str(quarter) in text

    def test_horizon_is_in_the_title(self):
        assert "T+10" in render(_report(), 10)
        assert "T+5" in render(_report(), 5)

    def test_header_carries_the_evaluated_window_not_the_fetch_start(self):
        """报告头必须写实际评估区间：飞书推送只有这一行能说清样本范围。

        实测取 402 天只评估了 271 天，而 271 天读「期望为零」、513 天读「负贡献」。
        表格里只写取数起点会让读者把没评估过的半年当成已覆盖。
        """
        text = render(
            _report(),
            10,
            {
                "market_start": 20240102,
                "eval_start": 20240704,
                "eval_end": 20260813,
                "market_days": 644,
                "eval_days": 513,
            },
        )
        assert "20240704~20260813" in text
        assert "513 天" in text
        # 取数起点也要在，但要与评估区间分开写，不能只有它。
        assert "20240102" in text

    def test_header_says_so_when_the_window_is_missing(self):
        """老产物没有 eval_window。这时要显式说「未记录」，不能静默留白。"""
        text = render(_report(), 10, None)
        assert "未记录" in text


class TestReportSerialization:
    def test_json_serializable(self):
        payload = json.loads(json.dumps(_report().as_dict(), ensure_ascii=False))
        assert payload["production"]["rps_fast_min"] == PROD_RPS_FAST_MIN
        assert payload["thresholds"][1]["is_production"] is True
        assert payload["thresholds"][1]["inside_t"] == pytest.approx(0.96)
        assert payload["switches"][0]["verdict"] == "走前不显著：不可上线"

    def test_quarter_keys_are_strings_and_sorted(self):
        payload = _report().as_dict()
        keys = list(payload["thresholds"][0]["by_quarter"])
        assert keys == sorted(keys)
        assert all(isinstance(k, str) for k in keys)

    def test_reading_note_states_both_conditions(self):
        reading = _report().as_dict()["reading"]
        assert "excess_t" in reading
        assert "绝对收益的 t 值" in reading

    def test_reading_note_explains_the_control(self):
        reading = _report().as_dict()["reading"]
        assert "避开了顶部" in reading
        assert f"{NON_TOP_CAP:.0f} 分位" in reading

    def test_control_gap_is_serialized(self):
        payload = json.loads(json.dumps(_report().as_dict(), ensure_ascii=False))
        gap = payload["control_gap"]
        assert gap["seeds"] == len(CONTROL_SEEDS)
        assert "落在" in gap["verdict"]
        assert len(payload["controls"]) == len(CONTROL_SEEDS)

    def test_empty_report_is_safe(self):
        payload = MomentumReport().as_dict()
        assert payload["thresholds"] == []
        assert payload["mid_band"] is None
        assert payload["controls"] == []
        assert payload["control_gap"]["verdict"] == "样本不足"
        assert math.isclose(payload["production"]["rps_slow_min"], PROD_RPS_SLOW_MIN)
