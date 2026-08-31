"""Tests for momentum regime evaluation (RPS gate value + dynamic-switch designs)."""

from __future__ import annotations

import json
import math
import random

import pytest

from core.momentum_regime_eval import (
    MIN_DAYS,
    PROD_RPS_FAST_MIN,
    PROD_RPS_SLOW_MIN,
    BandStat,
    MomentumReport,
    SwitchStat,
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

    def test_significant_diff_is_flagged_for_further_work(self):
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
        assert stat.verdict == "走前显著：值得进一步验证"

    def test_on_rate_by_quarter_is_reported(self):
        """坏季度的开启率是否真的更低,是判断切换设计能不能识别水温的关键。"""
        rows = _switch_rows([float(i) for i in range(80)], start=20250100)
        rows += _switch_rows([0.0] * 80, start=20250400)
        stat = walk_forward_switch("x", rows, state_key="state", warmup=60)
        assert stat.on_rate_by_quarter[20251] == pytest.approx(1.0)
        assert stat.on_rate_by_quarter[20252] == pytest.approx(0.0)


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


def _report(*, prod_inside_t: float = 0.96, domain_inside_t: float = 1.27, switch_t: float = 0.27) -> MomentumReport:
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
    report.mid_band = _band("中动量档", inside=0.458, inside_t=1.10, excess=0.096, excess_t=1.10)
    report.domain = _band("流动性域内基准", inside=0.362, inside_t=domain_inside_t, excess=0.0, excess_t=0.0)
    report.domain.by_quarter = {}
    report.switches = [
        SwitchStat("按市场宽度切换", 282, 0.472, 0.432, 0.040, switch_t, 0.38, {20262: 0.35, 20263: 0.43}),
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

    def test_no_switch_survives_means_keep_fixed(self):
        text = render(_report(switch_t=0.27), 10)
        assert "维持固定放行" in text
        assert "样本内回看显著不算数" in text

    def test_surviving_switch_is_named(self):
        text = render(_report(switch_t=2.40), 10)
        assert "走前显著" in text
        assert "按市场宽度切换" in text

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

    def test_empty_report_is_safe(self):
        payload = MomentumReport().as_dict()
        assert payload["thresholds"] == []
        assert payload["mid_band"] is None
        assert math.isclose(payload["production"]["rps_slow_min"], PROD_RPS_SLOW_MIN)
