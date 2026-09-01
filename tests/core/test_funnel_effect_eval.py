"""Tests for funnel stock-picking effectiveness (matched control + random negative control)."""

from __future__ import annotations

import random
from statistics import mean

import pytest

from core.funnel_effect_eval import (
    CONTROL_SEEDS,
    MIN_DAYS,
    MIN_HITS_PER_DAY,
    MOM_MATCH_TOL_PCT,
    GroupStat,
    Panels,
    control_gap,
    evaluate_daily,
    match_by_momentum,
    resolve_layer,
    sample_momentum_band,
    summarize_group,
    tstat,
)
from core.pattern_forward_eval import ROUND_TRIP_COST_PCT


def _daily(pairs: list[tuple[float, float]], *, size: float = 10.0, start: str = "2026-06-") -> list[dict]:
    """逐日观测：(net, control)。日期都落在 2026Q2,便于季度分组的用例单独构造。"""
    return [
        {"date": f"{start}{i + 1:02d}", "size": size, "net": net, "control": ctl, "residual_mom": 0.0}
        for i, (net, ctl) in enumerate(pairs)
    ]


class TestTstat:
    def test_known_series(self):
        # 均值 3、样本标准差 sqrt(2.5)、n=5 -> 3 / sqrt(2.5/5) = 4.2426
        assert tstat([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(4.2426, abs=1e-3)

    def test_zero_variance_is_none(self):
        """常数序列的 t 无定义,不能返回 inf 混进报告。"""
        assert tstat([2.0, 2.0, 2.0]) is None

    def test_too_few_points_is_none(self):
        assert tstat([1.0, 2.0]) is None


class TestSummarizeGroup:
    def test_requires_minimum_days(self):
        """样本不足返回 None 而非 0.0。

        既有 pattern_forward_eval 在这里返回 0.0,报告渲染成「日均命中 0.0」,
        看起来像「一只都没匹配上」而不是「样本太少」——本轮排查在这上面绕了一圈。
        """
        stat = summarize_group("x", _daily([(1.0, 0.0)] * (MIN_DAYS - 1)))
        assert stat.days == MIN_DAYS - 1
        assert stat.excess_pct is None
        assert stat.net_pct is None
        assert stat.excess_t is None

    def test_equal_weights_days_not_symbols(self):
        """按交易日等权：命中 900 只的那天不该主导均值。"""
        rows = [
            {"date": "2026-06-01", "size": 900.0, "net": 10.0, "control": 0.0, "residual_mom": 0.0},
            *_daily([(0.0, 0.0)] * (MIN_DAYS - 1), size=3.0),
        ]
        assert summarize_group("x", rows).excess_pct == pytest.approx(10.0 / MIN_DAYS)

    def test_drops_days_below_min_hits(self):
        """只数不足的日子整天丢掉：3 只以下的等权均值噪声太大。"""
        rows = _daily([(1.0, 0.0)] * MIN_DAYS) + [
            {"date": "2026-06-99", "size": MIN_HITS_PER_DAY - 1, "net": 99.0, "control": 0.0, "residual_mom": 0.0}
        ]
        stat = summarize_group("x", rows)
        assert stat.days == MIN_DAYS
        assert stat.excess_pct == pytest.approx(1.0)

    def test_skips_rows_missing_either_side(self):
        rows = _daily([(1.0, 0.0)] * MIN_DAYS) + [
            {"date": "2026-06-98", "size": 10.0, "net": None, "control": 0.0, "residual_mom": 0.0},
            {"date": "2026-06-97", "size": 10.0, "net": 1.0, "control": None, "residual_mom": 0.0},
        ]
        assert summarize_group("x", rows).days == MIN_DAYS

    def test_excess_is_net_minus_control(self):
        stat = summarize_group("x", _daily([(-2.0, -5.0)] * MIN_DAYS))
        assert stat.net_pct == pytest.approx(-2.0)
        assert stat.control_pct == pytest.approx(-5.0)
        assert stat.excess_pct == pytest.approx(3.0)

    def test_positive_day_pct_counts_days_not_magnitude(self):
        """为正日比例要能揭穿「一天暴赚撑起均值」：这里均值为正但只有一半日子为正。"""
        pairs = [(9.0, 0.0), (-1.0, 0.0)] * (MIN_DAYS // 2)
        stat = summarize_group("x", _daily(pairs))
        assert stat.excess_pct > 0
        assert stat.positive_day_pct == pytest.approx(50.0)

    def test_groups_excess_by_quarter(self):
        rows = _daily([(2.0, 0.0)] * MIN_DAYS, start="2026-06-")
        rows += _daily([(-2.0, 0.0)] * MIN_DAYS, start="2026-08-")
        stat = summarize_group("x", rows)
        assert stat.by_quarter[20262] == pytest.approx(2.0)
        assert stat.by_quarter[20263] == pytest.approx(-2.0)

    def test_reports_residual_momentum(self):
        """残差动量必须出报告：配对是否真的中性化了动量,只能靠它检查,不能假定。"""
        rows = _daily([(1.0, 0.0)] * MIN_DAYS)
        for row in rows:
            row["residual_mom"] = 0.4
        assert summarize_group("x", rows).residual_mom_pct == pytest.approx(0.4)


class TestMatchByMomentum:
    def test_picks_nearest_neighbour(self):
        mom = {"h1": 10.0, "c_far": 12.5, "c_near": 10.2}
        pairs = match_by_momentum(["h1"], ["c_far", "c_near"], mom)
        assert pairs == [("h1", "c_near")]

    def test_rejects_beyond_tolerance(self):
        """动量差 10 个点的票不是对照。宁可少算几只,也不能拿它凑样本。"""
        mom = {"h1": 10.0, "c1": 10.0 + MOM_MATCH_TOL_PCT + 0.1}
        assert match_by_momentum(["h1"], ["c1"], mom) == []

    def test_tolerance_boundary_is_inclusive(self):
        mom = {"h1": 10.0, "c1": 10.0 + MOM_MATCH_TOL_PCT}
        assert match_by_momentum(["h1"], ["c1"], mom) == [("h1", "c1")]

    def test_without_replacement(self):
        """同一只对照股不能被两只候选共用,否则它的特异噪声会被放大一倍。"""
        mom = {"h1": 10.0, "h2": 10.1, "c1": 10.05, "c2": 11.0}
        pairs = match_by_momentum(["h1", "h2"], ["c1", "c2"], mom)
        assert len(pairs) == 2
        assert len({c for _, c in pairs}) == 2

    def test_exhausted_pool_drops_remaining_hits(self):
        mom = {"h1": 10.0, "h2": 10.1, "c1": 10.0}
        pairs = match_by_momentum(["h1", "h2"], ["c1"], mom)
        assert len(pairs) == 1

    def test_skips_hits_without_momentum(self):
        """20 日动量算不出来（上市不足 20 日）的候选直接跳过,不能当 0 处理。"""
        mom = {"h_ok": 10.0, "c1": 10.0, "c2": 10.1}
        pairs = match_by_momentum(["h_ok", "h_no_mom"], ["c1", "c2"], mom)
        assert [h for h, _ in pairs] == ["h_ok"]

    def test_is_deterministic(self):
        rng = random.Random(1)
        mom = {f"h{i}": rng.gauss(5.0, 8.0) for i in range(20)}
        mom.update({f"c{i}": rng.gauss(5.0, 8.0) for i in range(60)})
        hits, pool = [f"h{i}" for i in range(20)], [f"c{i}" for i in range(60)]
        assert match_by_momentum(hits, pool, mom) == match_by_momentum(hits, pool, mom)

    def test_residual_momentum_is_near_zero(self):
        """配对后候选与对照的平均动量差必须接近 0——这是整个口径成立的前提。"""
        rng = random.Random(7)
        # 候选动量右偏（真实形态：漏斗偏爱强势股）,对照池覆盖全区间。
        mom = {f"h{i}": 8.0 + abs(rng.gauss(0.0, 6.0)) for i in range(60)}
        mom.update({f"c{i}": rng.gauss(4.0, 12.0) for i in range(900)})
        hits, pool = [f"h{i}" for i in range(60)], [f"c{i}" for i in range(900)]
        pairs = match_by_momentum(hits, pool, mom)
        residual = mean(mom[h] for h, _ in pairs) - mean(mom[c] for _, c in pairs)
        assert abs(residual) < 0.5


class TestSampleMomentumBand:
    def test_stays_inside_the_neighbourhood(self):
        """逐只在 ±tol 邻域内抽。第一版从候选动量 [min,max] 均匀抽,残差动量
        高达 +6.1~+8.8pct——候选动量右偏,均匀抽出的控制组是个动量低 8 个点的
        更弱对手,它的「超额」里混着动量差,拿来跟配对超额比是错的。
        """
        mom = {"h1": 10.0, "near": 11.0, "far": 30.0}
        picked = sample_momentum_band(["h1"], ["near", "far"], mom, seed=11, date="2026-06-01")
        assert picked == ["near"]

    def test_residual_momentum_is_near_zero_like_matching(self):
        """随机控制的残差动量也要归零：控制组与配对组唯一的差别应是「邻域内选哪一只」。"""
        rng = random.Random(3)
        mom = {f"h{i}": 8.0 + abs(rng.gauss(0.0, 6.0)) for i in range(60)}
        mom.update({f"c{i}": rng.gauss(4.0, 12.0) for i in range(900)})
        hits, pool = [f"h{i}" for i in range(60)], [f"c{i}" for i in range(900)]
        for seed in CONTROL_SEEDS:
            band = sample_momentum_band(hits, pool, mom, seed=seed, date="2026-06-01")
            residual = mean(mom[h] for h in hits[: len(band)]) - mean(mom[c] for c in band)
            assert abs(residual) < 3.0, f"seed={seed} 残差动量 {residual:.2f}"

    def test_without_replacement(self):
        mom = {"h1": 10.0, "h2": 10.1, "c1": 10.0, "c2": 10.2}
        picked = sample_momentum_band(["h1", "h2"], ["c1", "c2"], mom, seed=11, date="2026-06-01")
        assert len(picked) == len(set(picked)) == 2

    def test_empty_neighbourhood_drops_the_hit(self):
        mom = {"h1": 10.0, "c1": 40.0}
        assert sample_momentum_band(["h1"], ["c1"], mom, seed=11, date="2026-06-01") == []

    def test_seeds_differ_but_each_is_reproducible(self):
        rng = random.Random(5)
        mom = {f"h{i}": rng.gauss(5.0, 3.0) for i in range(30)}
        mom.update({f"c{i}": rng.gauss(5.0, 3.0) for i in range(300)})
        hits, pool = [f"h{i}" for i in range(30)], [f"c{i}" for i in range(300)]
        a = sample_momentum_band(hits, pool, mom, seed=11, date="2026-06-01")
        b = sample_momentum_band(hits, pool, mom, seed=23, date="2026-06-01")
        assert a == sample_momentum_band(hits, pool, mom, seed=11, date="2026-06-01")
        assert a != b

    def test_date_enters_the_seed(self):
        """种子按 (seed, date) 混合,否则所有日子共用一次抽样序列,5 个种子量不出真实宽度。"""
        rng = random.Random(5)
        mom = {f"h{i}": rng.gauss(5.0, 3.0) for i in range(30)}
        mom.update({f"c{i}": rng.gauss(5.0, 3.0) for i in range(300)})
        hits, pool = [f"h{i}" for i in range(30)], [f"c{i}" for i in range(300)]
        d1 = sample_momentum_band(hits, pool, mom, seed=11, date="2026-06-01")
        d2 = sample_momentum_band(hits, pool, mom, seed=11, date="2026-06-02")
        assert d1 != d2


class TestResolveLayer:
    DAY = {"all": ["a", "b", "c", "d"], "formal_l4": ["a", "b"]}
    UNIVERSE = {"a", "b", "c", "d", "x", "y"}

    def test_formal_l4_control_pool_excludes_all_candidates(self):
        """对照池要排掉整个宽池,不只排 L4。

        早先按 ``universe - hits`` 建池,把「进了宽池但没进 L4」的票也当成了非候选,
        L4 的超额被自家兄弟稀释：口径修正后 T+5 从 +0.86 变 +0.42。
        """
        hits, pool = resolve_layer(self.DAY, self.UNIVERSE, "formal_l4")
        assert hits == ["a", "b"]
        assert pool == ["x", "y"]

    def test_all_layer_measures_the_wide_pool(self):
        hits, pool = resolve_layer(self.DAY, self.UNIVERSE, "all")
        assert hits == ["a", "b", "c", "d"]
        assert pool == ["x", "y"]

    def test_l4_vs_rest_控制池在宽池内(self):
        """最锐的一层：两组都过了宽池入口,差别只在 L4 这道筛。"""
        hits, pool = resolve_layer(self.DAY, self.UNIVERSE, "l4_vs_rest")
        assert hits == ["a", "b"]
        assert pool == ["c", "d"]

    def test_restricted_to_liquid_universe(self):
        """流动性池外的候选不参与：买不进的票算出来的收益没有意义。"""
        hits, pool = resolve_layer(self.DAY, {"a", "c", "x"}, "l4_vs_rest")
        assert hits == ["a"]
        assert pool == ["c"]

    def test_missing_keys_are_safe(self):
        assert resolve_layer({}, self.UNIVERSE, "l4_vs_rest") == ([], [])


def _panels(n_days: int = 40, *, codes: list[str] | None = None) -> Panels:
    codes = codes or [f"s{i}" for i in range(200)]
    dates = [f"2026-06-{i + 1:02d}" for i in range(n_days)]
    rng = random.Random(19)
    mom = {c: rng.gauss(5.0, 8.0) for c in codes}
    return Panels(
        open={d: dict.fromkeys(codes, 100.0) for d in dates},
        close={d: dict.fromkeys(codes, 100.0) for d in dates},
        liquid={d: set(codes) for d in dates},
        mom20={d: dict(mom) for d in dates},
        dates=dates,
    )


class TestPanels:
    def test_window_buys_next_open(self):
        """漏斗信号收盘后才出,最早的真实买点是 T+1 开盘。"""
        panels = _panels(10)
        assert panels.window("2026-06-01", 5) == ("2026-06-02", "2026-06-07")

    def test_window_returns_none_past_the_end(self):
        panels = _panels(10)
        assert panels.window("2026-06-09", 5) is None
        assert panels.window("2026-05-30", 5) is None

    def test_gross_return_is_equal_weighted(self):
        panels = _panels(5, codes=["a", "b"])
        panels.close["2026-06-03"] = {"a": 110.0, "b": 90.0}
        assert panels.gross_return(["a", "b"], "2026-06-02", "2026-06-03") == pytest.approx(0.0)
        assert panels.gross_return(["a"], "2026-06-02", "2026-06-03") == pytest.approx(10.0)

    def test_gross_return_skips_missing_and_zero_prices(self):
        panels = _panels(5, codes=["a", "b"])
        panels.open["2026-06-02"] = {"a": 100.0, "b": 0.0}
        panels.close["2026-06-03"] = {"a": 110.0, "b": 90.0}
        assert panels.gross_return(["a", "b", "ghost"], "2026-06-02", "2026-06-03") == pytest.approx(10.0)

    def test_gross_return_none_when_nothing_priced(self):
        panels = _panels(5, codes=["a"])
        assert panels.gross_return(["ghost"], "2026-06-02", "2026-06-03") is None


class TestEvaluateDaily:
    def _cands(self, panels: Panels, n_hits: int = 20, n_wide: int = 60) -> dict[str, dict]:
        codes = sorted(next(iter(panels.liquid.values())))
        return {
            d: {"formal_l4": codes[:n_hits], "all": codes[:n_wide]}
            for d in panels.dates[:-25]  # 留出足够的前向窗口
        }

    def test_matched_and_controls_share_the_same_hits(self):
        """随机控制必须与配对组用同一批候选,否则分母不同、两个超额不可比。

        这是本轮自己写出来又改掉的 bug：配对失败的候选留在了随机组里。
        """
        panels = _panels(60)
        rows = evaluate_daily(self._cands(panels), panels, 5, status="formal_l4")
        assert rows["matched"], "配对组应有观测"
        for seed in CONTROL_SEEDS:
            ctrl = rows[f"control_{seed}"]
            assert ctrl
            by_date = {r["date"]: r for r in ctrl}
            for row in rows["matched"]:
                # 同一天两边的 net 必须完全相同——它们算的是同一批票。
                assert by_date[row["date"]]["net"] == pytest.approx(row["net"])

    def test_cost_is_deducted_from_both_sides(self):
        """两边同扣往返成本,比较的是选股而非交易频率;超额里成本自然抵消。"""
        panels = _panels(60)
        rows = evaluate_daily(self._cands(panels), panels, 5, status="formal_l4")
        row = rows["matched"][0]
        # 全市场价格恒定 -> 毛收益 0 -> 净收益 = -成本,两边都是。
        assert row["net"] == pytest.approx(-ROUND_TRIP_COST_PCT)
        assert row["control"] == pytest.approx(-ROUND_TRIP_COST_PCT)

    def test_skips_days_below_min_hits(self):
        panels = _panels(60)
        cands = {d: {"formal_l4": ["s0", "s1"], "all": ["s0", "s1"]} for d in panels.dates[:-25]}
        assert evaluate_daily(cands, panels, 5, status="formal_l4")["matched"] == []

    def test_skips_days_without_a_full_window(self):
        panels = _panels(30)
        codes = sorted(next(iter(panels.liquid.values())))
        cands = {d: {"formal_l4": codes[:20], "all": codes[:60]} for d in panels.dates}
        rows = evaluate_daily(cands, panels, 20, status="formal_l4")
        # 20 日窗口需要 T+21,最后 21 天出不了观测。
        assert len(rows["matched"]) <= len(panels.dates) - 21

    def test_layer_flows_through(self):
        """status 直达 resolve_layer：l4_vs_rest 的对照池在宽池内,只数受宽池限制。"""
        panels = _panels(60)
        cands = self._cands(panels, n_hits=20, n_wide=25)
        rows = evaluate_daily(cands, panels, 5, status="l4_vs_rest")
        assert rows["matched"]
        # 对照池只有 5 只 -> 每天最多配 5 对。
        assert max(r["size"] for r in rows["matched"]) <= 5


class TestControlGap:
    """随机负控制：漏斗的超额到底是选股信息,还是只是「站在了某个动量位置上」。

    实测 L4 vs 同动量非 L4 候选 T+10 超额 +4.10pct、t=+3.55、为正日 70%,看着很强;
    但同口径随机控制给 +4.48~+5.17,配对超额反而在区间之下。缺这个对照就会把
    动量选位读成选股能力。
    """

    def _stat(self, excess: float | None) -> GroupStat:
        return GroupStat("x", 30, 13.7, None, None, excess, 3.55, 70.0, -0.16)

    def test_inside_control_range_has_no_selection_value(self):
        gap = control_gap(self._stat(4.098), [self._stat(e) for e in (4.477, 4.866, 5.165)])
        assert "落在" in gap["verdict"]
        assert gap["control_excess_min"] == pytest.approx(4.477)
        assert gap["control_excess_max"] == pytest.approx(5.165)
        assert gap["gap"] < 0

    def test_below_every_seed_is_still_not_a_win(self):
        """配对超额低于所有种子时更不能说有效——gap 为负,必然落进「无选股信息」。"""
        gap = control_gap(self._stat(1.588), [self._stat(e) for e in (1.594, 1.898, 2.272)])
        assert "落在" in gap["verdict"]
        assert gap["gap"] == pytest.approx(1.588 - 1.921, abs=1e-3)

    def test_gap_within_seed_spread_is_not_a_win(self):
        """哪怕高于所有种子,只要差距不超过种子间宽度就算不上跑赢。"""
        gap = control_gap(self._stat(0.35), [self._stat(e) for e in (0.10, 0.30)])
        assert gap["gap"] == pytest.approx(0.15)
        assert gap["seed_spread"] == pytest.approx(0.20)
        assert "落在" in gap["verdict"]

    def test_gap_equal_to_spread_is_not_a_win(self):
        """边界取 <=：刚好等于宽度不算跑赢。

        取二进制可精确表示的值（0.25/0.75/1.25）,否则 0.1/0.3/0.4 那组在浮点上
        gap 比 spread 大一个 eps,考的就不是这条分支了。
        """
        # 均值 0.5、宽度 0.5 -> matched=1.0 时 gap 恰好等于 spread。
        gap = control_gap(self._stat(1.0), [self._stat(e) for e in (0.25, 0.75)])
        assert gap["gap"] == pytest.approx(gap["seed_spread"])
        assert "落在" in gap["verdict"]

    def test_clear_outperformance_is_flagged(self):
        gap = control_gap(self._stat(2.00), [self._stat(e) for e in (0.10, 0.12)])
        assert "跑赢" in gap["verdict"]

    def test_single_seed_is_insufficient(self):
        """单种子的一次抽样量不出边缘宽度,不能拿来判定。"""
        assert control_gap(self._stat(4.1), [self._stat(4.5)])["verdict"] == "样本不足"

    def test_missing_matched_excess_is_insufficient(self):
        assert control_gap(self._stat(None), [self._stat(0.1), self._stat(0.2)])["verdict"] == "样本不足"

    def test_ignores_seeds_without_excess(self):
        gap = control_gap(self._stat(0.35), [self._stat(0.10), self._stat(0.30), self._stat(None)])
        assert gap["seeds"] == 2
