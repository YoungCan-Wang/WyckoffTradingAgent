"""Tests for trigger point evaluation（`_trigger_score` 那六个硬编码分值，路径 B）。

与前两轮（dry_q / trigger_q）的区别：那两轮审 `watch_score` 里的权重，判定看 diff
正负；这一轮审的是分值**对应关系**，判定看置换百分位、秩相关、走前三格。

构造样本注意两件事：
1. 常数序列的 tstat 返回 None，verdict 退化成「样本不足」，测不到分支 —— 用 `_jit`。
2. `topn_mean` 与 `nlargest` 的差别只在**有并列**时显现，所以专门有一个并列夹具。
"""

from __future__ import annotations

import numpy as np
import pytest

from core.trigger_points_eval import (
    FLAT_POINTS,
    MIN_DAYS,
    PROD_POINTS,
    PROD_SOS_RESONANT,
    PROD_SOS_SINGLE,
    ROUND_TRIP_COST_PCT,
    TRIGGER_KINDS,
    KindStat,
    PermutationStat,
    PointsReport,
    RankCorrStat,
    SosResonanceStat,
    WalkForwardStat,
    decision,
    parse_kinds,
    path_b_score,
    permutation_tables,
    quarter_of,
    rank_correlation,
    render,
    summarize_arm,
    summarize_kind,
    summarize_permutation,
    summarize_quarters,
    topn_mean,
    tstat,
    walk_forward_narrow,
    walk_forward_table,
)


def _jit(i: int, amp: float = 0.02) -> float:
    """偶数长度下均值为零的小抖动，只为给序列一点方差。"""
    return amp if i % 2 else -amp


def _series(level: float, n: int = 60, amp: float = 0.02) -> list[float]:
    return [level + _jit(i, amp) for i in range(n)]


# --- path_b_score：必须逐行复现生产的加总方式 ---


def test_path_b_score_matches_production_single_kinds() -> None:
    for kind, points in PROD_POINTS.items():
        assert path_b_score(frozenset({kind}), PROD_POINTS) == pytest.approx(points)


def test_path_b_score_sos_alone_uses_single_tier() -> None:
    assert path_b_score(frozenset({"sos"}), PROD_POINTS) == pytest.approx(PROD_SOS_SINGLE)


def test_path_b_score_sos_resonance_replaces_tier_not_adds() -> None:
    """生产写的是 ``(50 if other_hits else 15)``：共振时整块换成 50，不是 15+50。"""
    score = path_b_score(frozenset({"sos", "lps"}), PROD_POINTS)
    assert score == pytest.approx(PROD_SOS_RESONANT + PROD_POINTS["lps"])
    assert score != pytest.approx(PROD_SOS_SINGLE + PROD_SOS_RESONANT + PROD_POINTS["lps"])


def test_path_b_score_accumulates_non_sos_kinds() -> None:
    kinds = frozenset({"spring", "evr", "compression"})
    expected = PROD_POINTS["spring"] + PROD_POINTS["evr"] + PROD_POINTS["compression"]
    assert path_b_score(kinds, PROD_POINTS) == pytest.approx(expected)


def test_path_b_score_empty_is_zero() -> None:
    assert path_b_score(frozenset(), PROD_POINTS) == 0.0
    assert path_b_score("", PROD_POINTS) == 0.0


def test_parse_kinds_handles_missing() -> None:
    assert parse_kinds(None) == frozenset()
    assert parse_kinds(float("nan")) == frozenset()
    assert parse_kinds("sos|lps") == frozenset({"sos", "lps"})


# --- topn_mean：这一组是本轮方法论的核心 ---


def _naive_topn(scores: np.ndarray, values: np.ndarray, top_n: int) -> float:
    """对照实现：等价于 ``DataFrame.nlargest``，按 index 顺序打破并列。"""
    order = np.argsort(-scores, kind="stable")[:top_n]
    return float(values[order].mean())


def test_topn_mean_differs_from_nlargest_when_tied() -> None:
    """并列桶里 index 顺序 = ts_code 字典序，会把对照稀释掉。

    夹具：3 只高分票 + 6 只同分并列票，取 top5。并列桶要贡献 2 个名额，但前 3 只
    并列票的收益全是 10、后 3 只全是 0。``nlargest`` 只吃到前两只（=10），
    按比例分权吃到全桶均值（=5）。
    """
    scores = np.array([9.0, 9.0, 9.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    values = np.array([1.0, 1.0, 1.0, 10.0, 10.0, 10.0, 0.0, 0.0, 0.0])
    tie_aware = topn_mean(scores, values, 5)
    naive = _naive_topn(scores, values, 5)
    assert tie_aware == pytest.approx((1.0 * 3 + 5.0 * 2) / 5)
    assert naive == pytest.approx((1.0 * 3 + 10.0 * 2) / 5)
    assert tie_aware != pytest.approx(naive)


def test_topn_mean_is_invariant_to_row_order() -> None:
    """按比例分权的结果不依赖行序 —— 这正是它可复现的原因。"""
    scores = np.array([5.0, 5.0, 5.0, 5.0, 9.0, 9.0])
    values = np.array([10.0, 0.0, 10.0, 0.0, 1.0, 1.0])
    base = topn_mean(scores, values, 3)
    rng = np.random.default_rng(7)
    for _ in range(5):
        perm = rng.permutation(len(scores))
        assert topn_mean(scores[perm], values[perm], 3) == pytest.approx(base)


def test_topn_mean_matches_plain_mean_without_ties() -> None:
    scores = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    assert topn_mean(scores, values, 3) == pytest.approx(2.0)


def test_topn_mean_guards_short_and_mismatched_input() -> None:
    scores = np.array([1.0, 2.0])
    assert topn_mean(scores, np.array([1.0, 2.0]), 5) is None
    assert topn_mean(scores, np.array([1.0]), 1) is None
    assert topn_mean(np.array([]), np.array([]), 1) is None
    assert topn_mean(scores, np.array([1.0, 2.0]), 0) is None


# --- 置换检验 ---


def test_permutation_tables_preserve_value_multiset() -> None:
    tables = permutation_tables(PROD_POINTS, n_perm=25, seed=1)
    assert len(tables) == 25
    want = sorted(PROD_POINTS.values())
    for table in tables:
        assert sorted(table.keys()) == sorted(PROD_POINTS.keys())
        assert sorted(table.values()) == pytest.approx(want)


def test_permutation_tables_are_seed_reproducible() -> None:
    assert permutation_tables(PROD_POINTS, n_perm=5, seed=3) == permutation_tables(PROD_POINTS, n_perm=5, seed=3)


def test_summarize_permutation_flags_anti_calibration() -> None:
    stat = summarize_permutation(10, -0.85, [-0.85] + [-0.4 + i * 0.001 for i in range(199)])
    assert stat.percentile is not None and stat.percentile <= 5.0
    assert "反校准" in stat.verdict


def test_summarize_permutation_flags_calibrated_when_at_top() -> None:
    stat = summarize_permutation(10, 0.9, [-0.4 + i * 0.001 for i in range(200)])
    assert stat.percentile is not None and stat.percentile >= 95.0
    assert "反校准" not in stat.verdict


def test_summarize_permutation_reports_no_signal_inside_band() -> None:
    stat = summarize_permutation(10, -0.5, [-1.0 + i * 0.005 for i in range(200)])
    assert stat.percentile is not None and 5.0 < stat.percentile < 95.0
    assert "反校准" not in stat.verdict


def test_summarize_permutation_needs_enough_permutations() -> None:
    stat = summarize_permutation(10, -0.5, [-0.4, -0.3])
    assert stat.percentile is None
    assert "样本不足" in stat.verdict or "不足" in stat.verdict


# --- 单类型超额与秩相关 ---


def test_summarize_kind_flags_significant_negative() -> None:
    stat = summarize_kind("sos", 15.0, _series(-0.8, n=60, amp=0.3), rows=4590)
    assert stat.excess is not None and stat.excess < 0
    assert stat.excess_t is not None and stat.excess_t < -2.0
    assert "负" in stat.verdict


def test_summarize_kind_short_sample_is_insufficient() -> None:
    stat = summarize_kind("lps", 30.0, _series(-0.5, n=5), rows=40)
    assert "样本不足" in stat.verdict


def test_rank_correlation_negative_when_points_invert_excess() -> None:
    """分值越高、超额越差 —— 这是本轮实测到的形态。"""
    kinds = [
        KindStat(kind=k, points=float(40 - i * 5), days=200, excess=-1.0 + i * 0.1, excess_t=-2.5)
        for i, k in enumerate(TRIGGER_KINDS)
    ]
    corr = rank_correlation(kinds)
    assert corr.corr is not None and corr.corr < 0
    assert "负相关" in corr.verdict


def test_rank_correlation_positive_when_aligned() -> None:
    kinds = [
        KindStat(kind=k, points=float(10 + i * 5), days=200, excess=-1.0 + i * 0.1, excess_t=-2.5)
        for i, k in enumerate(TRIGGER_KINDS)
    ]
    corr = rank_correlation(kinds)
    assert corr.corr is not None and corr.corr > 0
    assert "正相关" in corr.verdict


def test_rank_correlation_handles_missing_excess() -> None:
    kinds = [KindStat(kind=k, points=12.0, days=0, excess=None, excess_t=None) for k in TRIGGER_KINDS]
    corr = rank_correlation(kinds)
    assert corr.corr is None
    assert "样本不足" in corr.verdict


# --- sos 共振 ---


def test_sos_resonance_flags_reversed_bonus() -> None:
    """共振档超额差于单独档时，必须说清 15 -> 50 方向是反的。"""
    stat = SosResonanceStat(
        single_excess=-0.797,
        single_t=-2.49,
        single_rows=4590,
        resonant_excess=-1.007,
        resonant_t=-1.79,
        resonant_rows=833,
    )
    assert stat.gap is not None and stat.gap < 0
    assert "反" in stat.verdict


def test_sos_resonance_positive_gap_reads_as_supported() -> None:
    stat = SosResonanceStat(
        single_excess=-0.80,
        single_t=-2.5,
        single_rows=4590,
        resonant_excess=-0.20,
        resonant_t=-0.9,
        resonant_rows=833,
    )
    assert stat.gap is not None and stat.gap > 0
    assert "反" not in stat.verdict


# --- 排序臂 ---


def test_summarize_arm_uses_same_day_paired_diff() -> None:
    """配对差用同日相减：两条臂共同的日级波动应该被消掉。"""
    prod = [1.0 + i for i in range(40)]
    arm = [v + 0.5 for v in prod]
    stat = summarize_arm("flat", 10, arm, prod=prod)
    assert stat.vs_prod == pytest.approx(0.5)
    assert stat.vs_prod_t is None or stat.vs_prod_t > 10  # 配对后方差≈0，t 极大或退化


def _paired(level: float, n: int = 60, amp: float = 0.1) -> list[float]:
    """给配对差留方差的序列。

    两条臂若共用同一份抖动，配对差是常数、tstat 返回 None、beats_production 退化成
    None —— 测不到闸门。这里让相位跟 level 走，两条臂的抖动就不同步。
    """
    phase = int(abs(level) * 1000) % 5 + 1
    return [level + ((i * phase) % 7 - 3) * amp for i in range(n)]


def test_arm_beats_production_requires_cost_and_significance() -> None:
    strong = summarize_arm("flat", 10, _paired(0.5), prod=_paired(0.0))
    assert strong.vs_prod is not None and strong.vs_prod > ROUND_TRIP_COST_PCT
    assert strong.vs_prod_t is not None and strong.vs_prod_t >= 2.0
    assert strong.beats_production is True


def test_arm_below_cost_does_not_beat_production() -> None:
    """t 值再高，幅度小于往返成本 0.202% 也不算赢。"""
    tiny = summarize_arm("flat", 10, _paired(0.05, amp=0.001), prod=_paired(0.0, amp=0.001))
    assert tiny.vs_prod is not None and tiny.vs_prod < ROUND_TRIP_COST_PCT
    assert tiny.beats_production is False


def test_arm_without_production_has_no_verdict() -> None:
    stat = summarize_arm("prod", 10, _series(-0.8))
    assert stat.vs_prod is None
    assert stat.beats_production is None


# --- 走前挑表 ---


def _wf_series(best: str, n: int = 200) -> dict[str, list[float]]:
    """每张候选表一条日序列，只有 ``best`` 那条基准值为 1.0。

    抖动必须**逐表不同**，否则 chosen 与 fixed 的差是常数、方差为零、tstat 返回 None。
    """
    keys = ("prod", "flat", "no_res", "by_excess")

    def noise(i: int, key: str) -> float:
        return ((i * 37 + len(key) * 13) % 7 - 3) * 0.01

    return {k: [(1.0 if k == best else 0.0) + noise(i, k) for i in range(n)] for k in keys}


def test_walk_forward_requires_production_key() -> None:
    stat = walk_forward_table(10, list(range(200)), {"flat": [0.1] * 200}, horizon=10)
    assert stat.days == 0
    assert "样本不足" in stat.verdict


def test_walk_forward_picks_the_better_table() -> None:
    dates = [20250100 + i for i in range(200)]
    stat = walk_forward_table(10, dates, _wf_series("flat"), horizon=10)
    assert stat.top_pick == "flat"
    assert stat.picks_off_production is not None and stat.picks_off_production > 0.9
    assert stat.diff is not None and stat.diff > 0


def test_walk_forward_truncates_to_settled_history() -> None:
    """截到 T-H-1：H 越长，可用天数越少。用了 T-1 就是未来信息。"""
    dates = [20250100 + i for i in range(200)]
    short = walk_forward_table(10, dates, _wf_series("flat"), horizon=5)
    long_h = walk_forward_table(10, dates, _wf_series("flat"), horizon=40)
    assert short.days >= long_h.days


def test_walk_forward_near_miss_t_does_not_pass() -> None:
    """t=1.96 贴着双侧 5% 临界点，不许四舍五入放行 —— 否则这道闸等于没有。"""
    stat = WalkForwardStat(
        top_n=10,
        days=363,
        chosen=-0.74,
        fixed=-0.88,
        diff=0.14,
        diff_t=1.96,
        pick_dist={"flat": 0.9, "prod": 0.1},
    )
    assert "不显著" in stat.verdict
    assert "支持" not in stat.verdict or "不足以支持" in stat.verdict


def test_walk_forward_scattered_picks_block_deployment() -> None:
    """走前显著但候选散开 = 疑似拟合噪声，不给上线。"""
    stat = WalkForwardStat(
        top_n=10,
        days=363,
        chosen=-0.70,
        fixed=-0.88,
        diff=0.18,
        diff_t=3.1,
        pick_dist={"flat": 0.50, "no_res": 0.40, "by_excess": 0.10},
    )
    assert stat.is_concentrated is False
    assert stat.picks_off_production == pytest.approx(1.0)
    assert "散开" in stat.verdict


def test_walk_forward_concentrated_off_production_supports_change() -> None:
    stat = WalkForwardStat(
        top_n=10,
        days=363,
        chosen=-0.70,
        fixed=-0.88,
        diff=0.18,
        diff_t=3.1,
        pick_dist={"flat": 0.92, "prod": 0.08},
    )
    assert stat.is_concentrated is True
    assert "支持替换" in stat.verdict


def test_walk_forward_short_sample_is_insufficient() -> None:
    dates = [20250100 + i for i in range(MIN_DAYS)]
    stat = walk_forward_table(10, dates, _wf_series("flat", n=MIN_DAYS), horizon=10)
    assert "样本不足" in stat.verdict


# --- 走前收窄到两方 ---


def test_walk_forward_narrow_only_offers_two_tables() -> None:
    """收窄格只有 prod / flat 两方,不能悄悄把别的候选混进来。"""
    dates = [20250100 + i for i in range(200)]
    series = _wf_series("flat")
    stat = walk_forward_narrow(10, dates, series["prod"], series["flat"], horizon=10)
    assert set(stat.pick_dist) <= {"prod", "flat"}
    assert stat.top_pick == "flat"


def test_walk_forward_narrow_concentration_restates_the_diff_sign() -> None:
    """两方之间的集中度不是独立信息:哪张增量为正,集中度就指向它。

    所以它在这一格里不构成第三闸。注意也**不是恒等于 1.00** —— 个别截面里 prod
    的历史均值确实更高,实测四格有三格落在 0.94~0.98。
    """
    dates = [20250100 + i for i in range(200)]
    flat_wins = _wf_series("flat")
    prod_wins = _wf_series("prod")
    a = walk_forward_narrow(10, dates, flat_wins["prod"], flat_wins["flat"], horizon=10)
    b = walk_forward_narrow(10, dates, prod_wins["prod"], prod_wins["flat"], horizon=10)
    assert a.top_pick == "flat" and a.diff is not None and a.diff > 0
    assert b.top_pick == "prod" and b.diff is not None and b.diff <= 0
    # 集中度跟着增量符号走,两侧都「集中」,可见它不携带额外判据。
    assert a.is_concentrated is True and b.is_concentrated is True


def test_walk_forward_narrow_matches_two_table_walk_forward() -> None:
    """收窄格必须就是「候选集只有两张」的 walk_forward_table,不是另一套算法。"""
    dates = [20250100 + i for i in range(200)]
    series = _wf_series("flat")
    narrow = walk_forward_narrow(10, dates, series["prod"], series["flat"], horizon=10)
    direct = walk_forward_table(10, dates, {"prod": series["prod"], "flat": series["flat"]}, horizon=10)
    assert (narrow.days, narrow.diff, narrow.diff_t) == (direct.days, direct.diff, direct.diff_t)


def test_walk_forward_narrow_empty_series_is_insufficient() -> None:
    assert walk_forward_narrow(10, [], [], [], horizon=10).days == 0
    dates = [20250100 + i for i in range(200)]
    assert walk_forward_narrow(10, dates, _wf_series("flat")["prod"], [], horizon=10).days == 0


def test_narrow_finding_never_claims_three_gates_passed() -> None:
    """t 过了也只能说到「这 6 个自由参数不值」,不许升级成「三闸全过」。"""
    stat = WalkForwardStat(10, 363, -0.70, -0.88, 0.18, 3.1, {"flat": 1.0})
    report = _full_report()
    report.walk_forward_narrow = [stat]
    narrow = "\n".join(decision(report)).split("⑤")[1].split("⑥")[0]
    assert "支持删掉这张表" in narrow
    assert "不构成第三闸" in narrow
    # 「三闸全过」只许以被否认的形式出现,不许当成本格的结论。
    assert "不是「三闸全过」" in narrow
    assert narrow.count("三闸全过") == 1
    # 也不许把集中度说成「必然 1.00」—— 实测多数格是 0.94~0.98。
    assert "1.00" not in narrow


def test_narrow_finding_holds_production_when_t_misses() -> None:
    report = _full_report()
    report.walk_forward_narrow = [WalkForwardStat(10, 363, -0.74, -0.88, 0.14, 1.30, {"flat": 1.0})]
    text = "\n".join(decision(report))
    assert "收窄走前不显著" in text
    assert "维持生产" in text


def test_narrow_finding_reports_insufficient_sample() -> None:
    report = _full_report()
    report.walk_forward_narrow = [WalkForwardStat(10, 5, None, None, None, None, {})]
    assert "样本不足" in "\n".join(decision(report)).split("⑤")[1]


# --- 季度切片与工具 ---


def test_summarize_quarters_splits_by_quarter() -> None:
    dates = [20250115 + i for i in range(10)] + [20250715 + i for i in range(10)]
    diffs = [0.3 + _jit(i) for i in range(20)]
    quarters = summarize_quarters(dates, diffs)
    assert {q.quarter for q in quarters} == {20251, 20253}
    assert all(q.days == 10 for q in quarters)


def test_quarter_of_maps_month_to_quarter() -> None:
    assert quarter_of(20250115) == 20251
    assert quarter_of(20250415) == 20252
    assert quarter_of(20251231) == 20254


def test_tstat_returns_none_on_zero_variance() -> None:
    assert tstat([1.0] * 30) is None
    assert tstat([]) is None
    assert tstat([1.0]) is None


# --- 报告与结论 ---


def _full_report(*, wf_t: float = 1.08, pick_dist: dict[str, float] | None = None) -> PointsReport:
    report = PointsReport()
    report.arms = [
        summarize_arm("prod", 10, _series(-0.851, amp=0.3)),
        summarize_arm("flat", 10, _series(-0.512, amp=0.3), prod=_series(-0.851, amp=0.3)),
    ]
    report.permutations = [
        PermutationStat(
            top_n=10,
            n_perm=200,
            prod=-0.851,
            band_low=-0.851,
            band_high=-0.237,
            band_median=-0.487,
            percentile=0.5,
        )
    ]
    report.kinds = [
        KindStat(kind=k, points=float(34 - i * 4), days=400, excess=-1.0 + i * 0.1, excess_t=-2.5, rows=5000)
        for i, k in enumerate(TRIGGER_KINDS)
    ]
    report.rank_corr = RankCorrStat(top_n_note="单类型命中", corr=-0.435, n_kinds=6)
    report.sos = SosResonanceStat(-0.797, -2.49, 4590, -1.007, -1.79, 833)
    report.walk_forward = [
        WalkForwardStat(
            top_n=10,
            days=363,
            chosen=-0.740,
            fixed=-0.884,
            diff=0.144,
            diff_t=wf_t,
            pick_dist=pick_dist or {"flat": 0.50, "no_res": 0.40, "by_excess": 0.10},
        )
    ]
    report.quarters = summarize_quarters([20250115 + i for i in range(30)], [0.3 + _jit(i) for i in range(30)])
    report.unique_scores = 11
    report.tie_bucket_median = 23
    report.tie_bucket_max = 523
    return report


def test_decision_holds_production_when_walk_forward_fails() -> None:
    lines = decision(_full_report(wf_t=1.08))
    text = "\n".join(lines)
    assert "反校准" in text
    assert "暂维持" in text


def test_decision_flags_near_miss_without_rounding_up() -> None:
    """1.80 <= t < 2.0 要明确写「贴着线但没过」，不能读成通过。"""
    text = "\n".join(decision(_full_report(wf_t=1.96)))
    assert "暂维持" in text
    assert "不四舍五入" in text


def test_decision_warns_against_cutting_both_paths() -> None:
    text = "\n".join(decision(_full_report()))
    assert "路径 A" in text and "同时" in text


def test_decision_supports_change_when_all_gates_pass() -> None:
    text = "\n".join(decision(_full_report(wf_t=3.2, pick_dist={"flat": 0.92, "prod": 0.08})))
    assert "暂维持" not in text


def test_render_includes_all_sections_and_double_count_warning() -> None:
    text = render(_full_report(), horizon=10, start=20241118, end=20260813)
    for heading in ("排序臂对照", "置换检验", "并列", "sos 共振", "走前挑分值表", "结论"):
        assert heading in text
    assert "不可同时下调" in text
    assert "nlargest" in text
    assert str(FLAT_POINTS) in text or "25" in text


def test_render_narrow_section_disowns_its_concentration() -> None:
    """收窄格必须在报告正文里自己写清「集中度不构成证据」,否则下一轮会读成三闸全过。"""
    report = _full_report()
    report.walk_forward_narrow = [WalkForwardStat(10, 363, -0.70, -0.88, 0.18, 3.1, {"flat": 1.0})]
    text = render(report, horizon=10, start=20241118, end=20260813)
    assert "走前收窄到两方" in text
    assert "第三闸" in text and "不成立" in text
    assert "自由参数" in text
    # 表头不许有选中分布列 —— 印出来就等于邀请人把 1.00 当证据读。
    narrow = text.split("走前收窄到两方")[1].split("###")[0]
    header = next(line for line in narrow.splitlines() if line.startswith("| topN"))
    assert "选中分布" not in header
    assert "不看选中分布" in narrow


def test_report_as_dict_carries_narrow_caveat_into_json() -> None:
    """JSON 是给下一轮机器读的,警告必须跟着落盘,不能只写在 Markdown 里。"""
    report = _full_report()
    report.walk_forward_narrow = [WalkForwardStat(10, 363, -0.70, -0.88, 0.18, 3.1, {"flat": 1.0})]
    cell = report.as_dict()["walk_forward_narrow"]
    assert len(cell["cells"]) == 1
    assert "不构成证据" in cell["note"]
    assert "自由参数" in cell["note"]
    assert "1.00" not in cell["note"]


def test_report_as_dict_records_production_table_and_tie_note() -> None:
    payload = _full_report().as_dict()
    assert payload["production"]["points"] == dict(PROD_POINTS)
    assert payload["production"]["sos_resonant"] == PROD_SOS_RESONANT
    assert payload["production"]["source"] == "core/ai_candidate_allocation.py:559-582"
    assert payload["cost_threshold_pct"] == ROUND_TRIP_COST_PCT
    assert "nlargest" in payload["tie_break"]["note"]
    assert payload["tie_break"]["boundary_bucket_median"] == 23
