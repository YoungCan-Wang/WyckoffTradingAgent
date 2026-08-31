"""Tests for trigger weight evaluation (watch_score 的 trigger_q 分量)。

判定方向与 test_ranker_weight_eval 相反：那边 diff 为正才可行动（保留 dry_q），
这边 diff 为**负**才可行动（下调 trigger_q）。构造样本时注意别用常数序列——
方差为零的话 tstat 返回 None，verdict 会退化成「样本不足」，测不到分支。
"""

from __future__ import annotations

import pytest

from core.trigger_weight_eval import (
    MIN_DAYS,
    MIN_REPLAY_BARS,
    PROD_TRIGGER_WEIGHT,
    ROUND_TRIP_COST_PCT,
    TRIGGER_KINDS,
    WEIGHT_GRID,
    AblationStat,
    BinaryStat,
    KindStat,
    MagnitudeStat,
    PoolStat,
    TriggerReport,
    WalkForwardStat,
    decision,
    extension_penalty,
    production_detectors,
    quarter_of,
    render,
    replay_entry_bias_limit,
    summarize_ablation,
    summarize_binary,
    summarize_kind,
    summarize_magnitude,
    summarize_pool,
    summarize_weight,
    tstat,
    walk_forward_weight,
)


def _jit(i: int, amp: float = 0.02) -> float:
    """偶数长度下均值为零的小抖动，只为给序列一点方差。"""
    return amp if i % 2 else -amp


def _daily(excess: float, n: int = 40, size: float = 10.0, amp: float = 0.02) -> list[dict[str, float]]:
    return [{"inside": excess + _jit(i, amp), "domain": 0.0, "size": size} for i in range(n)]


def _daily_noise(n: int = 40) -> list[dict[str, float]]:
    return [{"inside": 1.0 if i % 2 else -1.0, "domain": 0.0, "size": 10.0} for i in range(n)]


def _ab(
    keep: float,
    drop: float = 0.0,
    n: int = 40,
    start: int = 20250106,
    binary: float | str | None = None,
    amp: float = 0.02,
) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for i in range(n):
        value = keep + _jit(i, amp)
        row = {"date": float(start + i), "keep": value, "drop": drop, "overlap": 0.1}
        if binary == "same":
            row["binary"] = value
        elif isinstance(binary, (int, float)):
            row["binary"] = float(binary)
        rows.append(row)
    return rows


def _wf_series(best: float, n: int = 200) -> dict[float, list[float]]:
    """每个权重一条日序列，只有 ``best`` 那条基准值是 1.0。

    抖动必须**逐权重不同**，否则 chosen 与 fixed 的差是常数、方差为零。
    """

    def noise(i: int, weight: float) -> float:
        return ((i * 37 + int(weight * 100) * 13) % 7 - 3) * 0.01

    return {w: [(1.0 if w == best else 0.0) + noise(i, w) for i in range(n)] for w in WEIGHT_GRID}


def _full_report(*, diff: float = -1.45, diff_t: float = -3.14, wf_t: float = 2.4) -> TriggerReport:
    report = TriggerReport()
    report.binary = BinaryStat(200, 134.0, -0.7, -4.0, -0.774, -4.67)
    report.magnitude = MagnitudeStat(200, -0.125, -0.53, -0.035, -2.86)
    report.kinds = [KindStat(k, 200, 20.0, -0.5 - i * 0.1, -2.5) for i, k in enumerate(TRIGGER_KINDS)]
    report.ablation = [
        AblationStat(
            n,
            200,
            -1.45,
            -4.1,
            0.01,
            0.03,
            diff,
            diff_t,
            binary=-1.4,
            binary_t=-4.1,
            keep_minus_binary=-0.04,
            keep_minus_binary_t=-0.26,
            overlap=0.113,
            rand_min=-0.778,
            rand_max=-0.081,
            excess_by_quarter={20254: -2.9, 20261: -2.0},
        )
        for n in (10, 20)
    ]
    report.walk_forward = [WalkForwardStat(n, 140, 1.3, 0.0, 1.28, wf_t, {0.0: 0.86, 0.1: 0.14}) for n in (10, 20)]
    report.pools = [PoolStat(300, 10, 200, 0.055, -1.4, -3.06)]
    report.kind_medians = {"sos": 4.285, "spring": 2.146}
    report.hits_dist = {1: 38060, 2: 1179, 3: 20}
    return report


class TestTstat:
    def test_returns_none_for_zero_variance(self):
        """方差为零时不返回 inf，否则会被误读成极显著。"""
        assert tstat([1.0] * 30) is None

    def test_needs_three_points(self):
        assert tstat([1.0, 2.0]) is None

    def test_matches_manual_calculation(self):
        assert tstat([1.0, 2.0, 3.0]) == pytest.approx(2.0 / (1.0 / 3.0**0.5))

    def test_ignores_non_finite(self):
        assert tstat([1.0, 2.0, 3.0, float("nan")]) == pytest.approx(tstat([1.0, 2.0, 3.0]))


class TestQuarterOf:
    def test_maps_month_to_quarter(self):
        assert quarter_of(20260115) == 20261
        assert quarter_of(20260401) == 20262
        assert quarter_of(20251231) == 20254


class TestExtensionPenalty:
    def test_zero_below_thresholds(self):
        assert extension_penalty(10.0, 5.0) == 0.0

    def test_caps_at_production_maxima(self):
        assert extension_penalty(500.0, 500.0) == pytest.approx(0.40)

    def test_monotone_in_ret20(self):
        assert extension_penalty(60.0, 0.0) > extension_penalty(50.0, 0.0)


class TestWeightGrid:
    def test_includes_zero_and_production(self):
        """必须含 0.0（等于删掉该项）与生产值，否则消融和网格读不出来。"""
        assert 0.0 in WEIGHT_GRID
        assert PROD_TRIGGER_WEIGHT in WEIGHT_GRID

    def test_extends_past_production(self):
        """网格要越过生产值，避免把边界值读成单调最优。"""
        assert max(WEIGHT_GRID) > PROD_TRIGGER_WEIGHT


class TestBinaryStat:
    def test_flags_significant_negative(self):
        stat = summarize_binary(_daily(-1.0))
        assert stat.excess == pytest.approx(-1.0)
        assert stat.verdict == "显著为负：触发本身是负超额"

    def test_flags_significant_positive(self):
        assert summarize_binary(_daily(1.0)).verdict == "显著为正：触发本身有超额"

    def test_insufficient_sample(self):
        stat = summarize_binary(_daily(1.0, n=3))
        assert stat.verdict == "样本不足"
        assert stat.excess is None

    def test_noise_is_not_significant(self):
        assert summarize_binary(_daily_noise()).verdict == "不显著：触发与否无差别"

    def test_reports_average_size(self):
        assert summarize_binary(_daily(-1.0, size=134.0)).avg_size == pytest.approx(134.0)


class TestMagnitudeStat:
    def test_flat_spread_reads_as_no_information(self):
        rows = [{"date": 20250106.0 + i, "spread": 0.01 if i % 2 else -0.01, "ic": 0.0} for i in range(40)]
        assert summarize_magnitude(rows).verdict == "不显著：幅度不带信息"

    def test_positive_spread(self):
        rows = [{"date": 20250106.0 + i, "spread": 1.0 + _jit(i), "ic": 0.05} for i in range(40)]
        stat = summarize_magnitude(rows)
        assert stat.verdict == "显著为正：幅度有判别力"
        assert stat.ic == pytest.approx(0.05)

    def test_negative_spread_flags_reversed_direction(self):
        rows = [{"date": 20250106.0 + i, "spread": -1.0 + _jit(i), "ic": -0.05} for i in range(40)]
        assert summarize_magnitude(rows).verdict == "显著为负：幅度方向反了"

    def test_missing_ic_is_tolerated(self):
        rows = [{"date": 20250106.0 + i, "spread": float(i % 3)} for i in range(40)]
        assert summarize_magnitude(rows).ic is None


class TestKindStat:
    def test_all_six_production_kinds_covered(self):
        assert len(TRIGGER_KINDS) == 6

    def test_negative_kind(self):
        assert summarize_kind("sos", _daily(-1.2)).verdict == "显著为负"

    def test_flat_kind(self):
        assert summarize_kind("sos", _daily_noise()).verdict == "不显著"

    def test_insufficient(self):
        assert summarize_kind("sos", _daily(-1.2, n=5)).verdict == "样本不足"


class TestAblationStat:
    def test_negative_diff_beyond_random_supports_cut(self):
        stat = summarize_ablation(10, _ab(-1.5), [-0.3, -0.2, -0.1])
        assert stat.diff == pytest.approx(-1.5)
        assert stat.worse_than_random is True
        assert stat.verdict == "显著为负且超出随机带：支持下调"

    def test_negative_inside_random_band_weakens_claim(self):
        """落在随机带内：不能归因给 trigger_q 本身，但也不支持 0.30。"""
        stat = summarize_ablation(10, _ab(-0.5), [-1.0, -0.8, -0.2])
        assert stat.worse_than_random is False
        assert "落在随机带内" in stat.verdict

    def test_tiny_negative_does_not_clear_cost(self):
        stat = summarize_ablation(10, _ab(-0.1, amp=0.005), [-0.05])
        assert abs(stat.diff or 0.0) <= ROUND_TRIP_COST_PCT
        assert stat.verdict == "显著为负但幅度不抵成本"

    def test_positive_diff_supports_keeping(self):
        assert summarize_ablation(10, _ab(1.5), [0.1]).verdict == "显著为正：支持保留"

    def test_flat_diff_is_not_worth_the_weight(self):
        stat = summarize_ablation(10, _ab(0.0, amp=0.01), [0.0])
        assert stat.verdict == "不显著：这一项不值 0.30 权重"

    def test_rank_adds_nothing_over_binary(self):
        """keep 与 binary 近乎相等 -> 0.30 的连续分位项是白给的复杂度。

        binary 臂给独立抖动而非直接复制 keep：完全相等会让 keep−binary 方差为零、
        t 退化成 None，读不出「不比二元旗标强」这个结论。
        """
        rows = _ab(-1.5, binary="same")
        for i, row in enumerate(rows):
            row["binary"] = row["keep"] + _jit(i, 0.3) - 0.04
        stat = summarize_ablation(10, rows, [-0.2])
        assert stat.keep_minus_binary == pytest.approx(0.04)
        assert stat.keep_minus_binary_t is not None and abs(stat.keep_minus_binary_t) < 2.0
        assert stat.rank_adds_over_binary is False

    def test_rank_beats_binary_when_clearly_better(self):
        stat = summarize_ablation(10, _ab(1.0, binary=0.0), [0.1])
        assert stat.rank_adds_over_binary is True

    def test_missing_binary_arm_yields_none(self):
        assert summarize_ablation(10, _ab(-1.5), [-0.2]).rank_adds_over_binary is None

    def test_negative_quarters_counted(self):
        rows = _ab(-1.0, n=20, start=20250106) + _ab(1.0, n=20, start=20250406)
        assert summarize_ablation(10, rows, [-0.1]).negative_quarters == "1/2"

    def test_missing_random_band_yields_none(self):
        assert summarize_ablation(10, _ab(-1.5), []).worse_than_random is None

    def test_insufficient_rows(self):
        assert summarize_ablation(10, _ab(-1.5, n=3), []).verdict == "样本不足"


class TestMergePanelCoverage:
    """回归：行情区间宽于触发面板时，必须裁到面板覆盖的日期。

    首次跑仓库版本时踩到过——面板 192 日、行情缓存 610 日，面板外的日子全票
    trigger_score 填 0，keep 臂与 drop 臂选出同一批票、配对差恒为 0，
    把 -1.46 稀释成 -0.43（正好是 192/610 的比例）。方向没错，幅度全错。
    """

    def _frames(self):
        pd = pytest.importorskip("pandas")
        from scripts.evaluate_trigger_weight import merge_panel

        feats = pd.DataFrame(
            {
                "ts_code": ["000001.SZ"] * 4,
                "trade_date": [20250106, 20250107, 20250108, 20250109],
                "fwd": [1.0, 2.0, 3.0, 4.0],
            }
        )
        panel = pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000001.SZ"],
                "trade_date": [20250108, 20250109],
                "trigger_score": [2.1, 0.6],
                "n_hits": [1, 1],
                "kinds": ["evr", "lps"],
            }
        )
        return merge_panel(feats, panel), pd

    def test_trims_to_panel_dates(self):
        merged, _ = self._frames()
        assert sorted(merged["trade_date"].tolist()) == [20250108, 20250109]

    def test_keeps_zero_fill_semantics_inside_coverage(self):
        """覆盖区间内未命中的票仍填 0.0，与生产 candidate_score_value(None) 一致。"""
        pd = pytest.importorskip("pandas")
        from scripts.evaluate_trigger_weight import merge_panel

        feats = pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ"],
                "trade_date": [20250108, 20250108],
                "fwd": [1.0, 2.0],
            }
        )
        panel = pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [20250108],
                "trigger_score": [2.1],
                "n_hits": [1],
                "kinds": ["evr"],
            }
        )
        merged = merge_panel(feats, panel)
        assert len(merged) == 2
        untriggered = merged[merged["ts_code"] == "000002.SZ"].iloc[0]
        assert untriggered["trigger_score"] == pytest.approx(0.0)
        assert untriggered["n_hits"] == 0
        assert untriggered["kinds"] == ""

    def test_empty_panel_leaves_features_untouched(self):
        pd = pytest.importorskip("pandas")
        from scripts.evaluate_trigger_weight import merge_panel

        feats = pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": [20250106], "fwd": [1.0]})
        panel = pd.DataFrame({"ts_code": [], "trade_date": [], "trigger_score": [], "n_hits": [], "kinds": []})
        assert len(merge_panel(feats, panel)) == 1


class TestWeightStat:
    def test_marks_production_weight(self):
        rows = _daily(1.0)
        assert summarize_weight(PROD_TRIGGER_WEIGHT, rows).is_production is True
        assert summarize_weight(0.0, rows).is_production is False


class TestPoolStat:
    def test_negative_survives_in_pool(self):
        rows = [{"keep": -1.4 + _jit(i), "drop": 0.0, "rate": 0.055} for i in range(40)]
        stat = summarize_pool(300, 10, rows)
        assert stat.verdict == "池内仍显著为负"
        assert stat.trigger_rate == pytest.approx(0.055)

    def test_positive_in_pool_flips_verdict(self):
        rows = [{"keep": 1.4 + _jit(i), "drop": 0.0, "rate": 0.055} for i in range(40)]
        assert summarize_pool(300, 10, rows).verdict == "池内转显著为正"

    def test_insufficient(self):
        assert summarize_pool(300, 10, []).verdict == "样本不足"


class TestWalkForwardMechanics:
    def test_picks_zero_when_zero_is_best(self):
        dates = list(range(20250106, 20250106 + 200))
        stat = walk_forward_weight(10, dates, _wf_series(0.0), horizon=10, warmup=60)
        assert stat.pick_dist == {0.0: pytest.approx(1.0)}
        assert stat.picks_below_production == pytest.approx(1.0)
        assert stat.diff is not None and stat.diff > 0
        assert "支持下调" in stat.verdict

    def test_no_gain_when_production_is_best(self):
        dates = list(range(20250106, 20250106 + 200))
        stat = walk_forward_weight(10, dates, _wf_series(PROD_TRIGGER_WEIGHT), horizon=10, warmup=60)
        assert stat.diff == pytest.approx(0.0)
        assert stat.picks_below_production == pytest.approx(0.0)

    def test_respects_settlement_lag(self):
        """H=10 时 T 日只能用截到 T-11 的历史，用不了未结算的前向收益。"""
        dates = list(range(20250106, 20250106 + 200))
        by_weight = {w: [float(i) for i in range(200)] for w in WEIGHT_GRID}
        assert walk_forward_weight(10, dates, by_weight, horizon=10, warmup=60).days == 140

    def test_longer_horizon_costs_more_days(self):
        dates = list(range(20250106, 20250106 + 200))
        by_weight = {w: [float(i) for i in range(200)] for w in WEIGHT_GRID}
        short = walk_forward_weight(10, dates, by_weight, horizon=5, warmup=60).days
        assert short == walk_forward_weight(10, dates, by_weight, horizon=10, warmup=60).days

    def test_missing_production_weight_bails(self):
        stat = walk_forward_weight(10, [1, 2, 3], {0.0: [1.0, 2.0, 3.0]}, horizon=5)
        assert stat.days == 0
        assert stat.verdict == "样本不足"


class TestWalkForwardVerdict:
    def test_concentration_flag(self):
        assert WalkForwardStat(10, 100, 1.0, 0.0, 1.0, 3.0, {0.0: 0.9, 0.1: 0.1}).is_concentrated is True
        assert WalkForwardStat(10, 100, 1.0, 0.0, 1.0, 3.0, {0.0: 0.5, 0.6: 0.5}).is_concentrated is False

    def test_insignificant_diff_blocks_change(self):
        stat = WalkForwardStat(10, 140, 1.0, 0.0, 0.2, 0.5, {0.0: 1.0})
        assert stat.verdict == "走前不显著：不足以支持改动"

    def test_scattered_below_production_still_supports_direction(self):
        """散在 0.0/0.10 之间但都低于生产值：方向稳，档位待定。"""
        stat = WalkForwardStat(10, 100, 1.0, 0.0, 1.0, 2.4, {0.0: 0.59, 0.1: 0.41})
        assert stat.is_concentrated is False
        assert stat.picks_below_production == pytest.approx(1.0)
        assert "支持下调，具体档位待定" in stat.verdict

    def test_scattered_across_production_is_noise(self):
        stat = WalkForwardStat(10, 100, 1.0, 0.0, 1.0, 2.4, {0.1: 0.34, 0.45: 0.33, 0.6: 0.33})
        assert "疑似拟合噪声" in stat.verdict

    def test_concentrated_above_production_is_not_a_cut(self):
        stat = WalkForwardStat(10, 100, 1.0, 0.0, 1.0, 2.4, {0.6: 1.0})
        assert stat.picks_below_production == pytest.approx(0.0)
        assert stat.verdict == "走前显著：值得进一步验证"

    def test_empty_pick_dist(self):
        stat = WalkForwardStat(10, 0, None, None, None, None, {})
        assert stat.is_concentrated is None
        assert stat.picks_below_production is None
        assert stat.verdict == "样本不足"


class TestDecision:
    def test_recommends_cut_when_all_gates_pass(self):
        lines = decision(_full_report())
        assert any("支持把 trigger_q 权重从" in line for line in lines)
        assert any("没有一种为正" in line for line in lines)

    def test_holds_when_walk_forward_fails(self):
        assert any("暂维持" in line for line in decision(_full_report(wf_t=0.5)))

    def test_near_miss_walk_forward_is_called_out_not_rounded_up(self):
        """t=1.96 贴着双侧 5% 临界点。423 日实跑就是这个值，必须挡住且写明贴线。

        差 0.04 就放行的话这道闸等于没有——首轮 192 日窗口给的是 t=+2.14，
        补到 423 日掉到 +1.96，正是「幅度有一半是单段行情运气」的表现。
        """
        lines = decision(_full_report(wf_t=1.96))
        action = next(line for line in lines if line.startswith("④"))
        assert "暂维持" in action
        assert "贴着线但没过" in action and "t=+1.96" in action
        assert "支持把 trigger_q 权重从" not in action

    def test_clearly_failed_walk_forward_gets_no_near_miss_note(self):
        action = next(line for line in decision(_full_report(wf_t=0.5)) if line.startswith("④"))
        assert "贴着线" not in action

    def test_holds_when_ablation_not_negative(self):
        lines = decision(_full_report(diff=0.01, diff_t=0.1))
        assert any("消融没有一格显著为负" in line for line in lines)

    def test_positive_kind_routes_to_reweighting(self):
        report = _full_report()
        report.kinds = [KindStat(k, 200, 20.0, 0.5 if i == 0 else -0.5, 2.5) for i, k in enumerate(TRIGGER_KINDS)]
        assert any("_trigger_score_map" in line and "分化" in line for line in decision(report))

    def test_empty_report_is_all_insufficient(self):
        assert any("样本不足" in line for line in decision(TriggerReport()))


class TestRender:
    def test_renders_all_sections(self):
        text = render(_full_report(), horizon=10, start=20240102, end=20260828)
        for heading in ("量纲体检", "Q1 二元", "Q2 幅度", "Q3 类型", "四臂消融", "权重网格", "L3 代理", "走前挑权重"):
            assert heading in text

    def test_marks_production_weight(self):
        report = _full_report()
        report.weights = {10: [summarize_weight(w, _daily(1.0)) for w in WEIGHT_GRID]}
        assert "← 生产" in render(report, horizon=10, start=20240102, end=20260828)

    def test_empty_report_renders(self):
        text = render(TriggerReport(), horizon=5, start=20240102, end=20260828)
        assert "样本不足" in text

    def test_records_lower_bound_caveat(self):
        """channel="" 重放使触发率成为下界，报告必须写明，否则下轮会误读。"""
        assert "下界" in render(TriggerReport(), horizon=5, start=20240102, end=20260828)

    def test_serializes_scale_check(self):
        payload = _full_report().as_dict()
        assert payload["scale_check"]["n_hits_dist"]["1"] == 38060

    def test_min_days_guard_is_documented(self):
        assert MIN_DAYS >= 20


class TestReplayWrappers:
    """重放包装层必须与生产 ``layer4_triggers`` 同源。

    这两个函数存在的唯一理由是绕开架构边界（scripts/ 不得引 core 私有成员），
    所以它们一旦与生产漂移，重放出的面板就不再代表生产,而单看脚本发现不了。
    """

    def test_detectors_match_production_layer4(self):
        from core import wyckoff_engine

        assert production_detectors() == (
            ("spring", wyckoff_engine._detect_spring),
            ("lps", wyckoff_engine._detect_lps),
            ("evr", wyckoff_engine._detect_evr),
            ("compression", wyckoff_engine._detect_compression),
            ("sos", wyckoff_engine._detect_sos),
            ("trend_pullback", wyckoff_engine._detect_trend_pullback),
        )

    def test_detectors_cover_every_trigger_kind(self):
        """六种触发一个不少：漏一种,那一类的超额就整段读不到。"""
        assert {kind for kind, _ in production_detectors()} == set(TRIGGER_KINDS)

    def test_entry_bias_limit_matches_production_formula(self):
        """与 layer4_triggers 里那行同式:channel=""、rps_slow=None。"""
        import numpy as np
        import pandas as pd

        from core.wyckoff_engine import FunnelConfig, _effective_entry_max_bias_200, _ret120_pct

        rng = np.random.default_rng(7)
        close = 10 + np.cumsum(rng.normal(0, 0.12, 300))
        frame = pd.DataFrame(
            {
                "date": np.arange(20240101, 20240101 + 300),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": rng.uniform(1e5, 3e5, 300),
            }
        )
        cfg = FunnelConfig()
        expected = _effective_entry_max_bias_200(
            "600000.SH", "", cfg, rps_slow=None, ret120_pct=_ret120_pct(frame, cfg)
        )
        assert replay_entry_bias_limit("600000.SH", frame, cfg) == expected

    def test_replay_bars_cover_the_longest_lookback(self):
        """检测器要看 200 日 MA;预热不够会把早段截面静默判成不触发。"""
        assert MIN_REPLAY_BARS >= 210
