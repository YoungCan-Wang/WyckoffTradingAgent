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
    summarize_absolute,
    summarize_group,
    tstat,
    win_control_gap,
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


def _abs_daily(nets: list[float], benches: list[float | None] | None = None, *, size: float = 10.0) -> list[dict]:
    """绝对口径的逐日观测。benches 为 None 表示整段无基准。"""
    return [
        {
            "date": f"2026-06-{i + 1:02d}",
            "size_abs": size,
            "net_abs": net,
            "bench": None if benches is None else benches[i],
        }
        for i, net in enumerate(nets)
    ]


class TestSummarizeAbsolute:
    def test_sample_floor_blocks_short_windows(self):
        stat = summarize_absolute(_abs_daily([1.0] * (MIN_DAYS - 1)))
        assert stat.net_pct is None
        assert stat.verdict == "样本不足"

    def test_thin_days_are_dropped_like_matched(self):
        """当日候选不足 MIN_HITS_PER_DAY 的日子不参与,与配对口径同一道门槛。"""
        rows = _abs_daily([1.0] * MIN_DAYS)
        rows[0]["size_abs"] = MIN_HITS_PER_DAY - 1
        assert summarize_absolute(rows).days == MIN_DAYS - 1

    def test_negative_absolute_is_called_out(self):
        """超额可能为正而仓位实亏,判定必须先看绝对收益本身。"""
        stat = summarize_absolute(_abs_daily([-2.0] * MIN_DAYS))
        assert stat.net_pct == pytest.approx(-2.0)
        assert stat.verdict == "绝对收益为负：这批票拿着是亏的"

    def test_positive_day_pct_counts_net_positive_days(self):
        """AbsoluteStat.positive_day_pct 是「正收益**日**占比」,不是股级胜率。

        它也不是 GroupStat 的「超额为正日」。三个名字相近的口径互不等价,股级胜率
        看 ``stock_win_pct``(守在 TestStockWinRate)。
        """
        stat = summarize_absolute(_abs_daily([1.0] * 15 + [-1.0] * 5))
        assert stat.positive_day_pct == pytest.approx(75.0)
        assert stat.worst_day_pct == pytest.approx(-1.0)
        assert stat.best_day_pct == pytest.approx(1.0)

    def test_beats_market_only_when_bench_excess_positive(self):
        """绝对为正但跑输基准 = 只赚了 beta,这正是纯度检验里 LPS T+40 的形态。"""
        rows = _abs_daily([1.0] * MIN_DAYS, [3.0] * MIN_DAYS)
        stat = summarize_absolute(rows)
        assert stat.bench_pct == pytest.approx(3.0)
        assert stat.bench_excess_pct == pytest.approx(-2.0)
        assert stat.verdict == "绝对为正但跑输基准：只赚了市场的钱"

        beat = summarize_absolute(_abs_daily([4.0] * MIN_DAYS, [3.0] * MIN_DAYS))
        assert beat.verdict == "绝对为正且跑赢基准"

    def test_missing_bench_days_are_not_treated_as_zero(self):
        """缺基准的日子若按 0 计,会把无基准段当成「基准不涨不跌」凭空造超额。"""
        benches: list[float | None] = [None] * 5 + [2.0] * MIN_DAYS
        stat = summarize_absolute(_abs_daily([1.0] * (5 + MIN_DAYS), benches))
        assert stat.days == 5 + MIN_DAYS
        assert stat.bench_days == MIN_DAYS
        assert stat.bench_excess_pct == pytest.approx(-1.0)

    def test_bench_columns_absent_without_benchmark(self):
        stat = summarize_absolute(_abs_daily([1.0] * MIN_DAYS))
        assert stat.bench_days == 0
        assert (stat.bench_pct, stat.bench_excess_pct, stat.bench_excess_t) == (None, None, None)
        assert stat.verdict == "绝对为正且跑赢基准"

    def test_bench_excess_needs_its_own_sample_floor(self):
        """基准日数不够时只压掉基准三列,绝对收益本身照出。"""
        benches: list[float | None] = [2.0] * (MIN_DAYS - 1) + [None] * 5
        stat = summarize_absolute(_abs_daily([1.0] * (MIN_DAYS + 4), benches))
        assert stat.net_pct == pytest.approx(1.0)
        assert stat.bench_days == MIN_DAYS - 1
        assert stat.bench_excess_pct is None


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

    def test_bench_return_spans_the_same_window_as_candidates(self):
        """基准必须 T+1 开盘进、T+1+H 收盘出。用 buy_ds 收盘当起点会把 T+1 的涨跌
        从基准里剔掉却留在候选里,跳空日能造出假超额。"""
        panels = _panels(5, codes=["a"])
        panels.bench_open = {"2026-06-02": 100.0}
        panels.bench_close = {"2026-06-02": 105.0, "2026-06-04": 110.0}
        assert panels.bench_return("2026-06-02", "2026-06-04") == pytest.approx(10.0)

    def test_bench_return_none_when_either_end_missing(self):
        panels = _panels(5, codes=["a"])
        panels.bench_open = {"2026-06-02": 100.0}
        panels.bench_close = {"2026-06-04": 110.0}
        assert panels.bench_return("2026-06-03", "2026-06-04") is None
        assert panels.bench_return("2026-06-02", "2026-06-05") is None

    def test_bench_return_none_on_nonpositive_start(self):
        panels = _panels(5, codes=["a"])
        panels.bench_open = {"2026-06-02": 0.0}
        panels.bench_close = {"2026-06-04": 110.0}
        assert panels.bench_return("2026-06-02", "2026-06-04") is None

    def test_bench_panels_default_to_empty(self):
        """没传基准时绝对收益照出,只是不出基准差额三列。"""
        panels = _panels(5)
        assert (panels.bench_open, panels.bench_close) == ({}, {})


class TestStockWinRate:
    """股级胜率:这批票里有多少只自己赚了钱。与日级口径不可混用。"""

    def _panels_with(self, rets: dict[str, float]) -> Panels:
        """按给定的逐只收益(%)造一个两日面板。"""
        codes = sorted(rets)
        return Panels(
            open={"2026-06-01": dict.fromkeys(codes, 100.0)},
            close={"2026-06-02": {c: 100.0 * (1.0 + rets[c] / 100.0) for c in codes}},
            liquid={"2026-06-01": set(codes)},
            mom20={"2026-06-01": dict.fromkeys(codes, 0.0)},
            dates=["2026-06-01", "2026-06-02"],
        )

    def test_counts_stocks_not_days(self):
        """3 只 +20% / 7 只 -5%:日级均值为正,股级只有 30%。这一格是两个口径的分水岭。"""
        rets = {f"w{i}": 20.0 for i in range(3)} | {f"l{i}": -5.0 for i in range(7)}
        panels = self._panels_with(rets)
        codes = sorted(rets)
        assert panels.stock_win_rate(codes, "2026-06-01", "2026-06-02") == pytest.approx(30.0)
        # 同一批票的日级均值确实为正,若把它当胜率就会读成「赢」。
        assert panels.gross_return(codes, "2026-06-01", "2026-06-02") > 0

    def test_cost_threshold_makes_thin_winners_losses(self):
        """毛涨 0.1% 扣掉往返成本是亏的,不能算赢。"""
        thin = ROUND_TRIP_COST_PCT / 2.0
        panels = self._panels_with({"a": thin, "b": thin})
        assert panels.stock_win_rate(["a", "b"], "2026-06-01", "2026-06-02") == pytest.approx(0.0)

        clear = ROUND_TRIP_COST_PCT * 2.0
        panels = self._panels_with({"a": clear, "b": clear})
        assert panels.stock_win_rate(["a", "b"], "2026-06-01", "2026-06-02") == pytest.approx(100.0)

    def test_missing_prices_are_dropped_not_counted_as_losses(self):
        """缺价的票不进分母,否则停牌会被记成亏。"""
        panels = self._panels_with({"a": 10.0})
        assert panels.stock_win_rate(["a", "ghost"], "2026-06-01", "2026-06-02") == pytest.approx(100.0)
        assert panels.stock_win_rate(["ghost"], "2026-06-01", "2026-06-02") is None

    def test_absolute_win_needs_min_days(self):
        """样本不足时给 None,不给一个看着像结论的数。"""
        short = [
            {"date": f"2026-06-{i + 1:02d}", "size_abs": 10, "net_abs": 1.0, "stock_win_abs": 60.0, "bench": None}
            for i in range(MIN_DAYS - 1)
        ]
        assert summarize_absolute(short).stock_win_pct is None
        enough = short + [{"date": "2026-07-01", "size_abs": 10, "net_abs": 1.0, "stock_win_abs": 60.0, "bench": None}]
        assert summarize_absolute(enough).stock_win_pct == pytest.approx(60.0)

    def test_group_win_excess_skips_days_missing_either_side(self):
        """缺一栏的日子不按 0 补,否则会凭空造出负超额。"""
        rows = [
            {
                "date": f"2026-06-{i + 1:02d}",
                "size": 10,
                "net": 1.0,
                "control": 0.0,
                "residual_mom": 0.0,
                "stock_win": 60.0,
                "stock_win_control": 50.0,
            }
            for i in range(MIN_DAYS)
        ]
        rows[0]["stock_win_control"] = None
        stat = summarize_group("m", rows)
        # 19 个可用日 < MIN_DAYS,故胜率一栏给 None,而收益一栏(20 日)照常有值。
        assert stat.stock_win_excess_pct is None
        assert stat.excess_pct == pytest.approx(1.0)


def _noisy_panels(n_days: int = 32, n_codes: int = 200) -> Panels:
    """行情带噪声、动量与噪声独立的面板。

    为什么必须带噪声：最近邻动量配对**天然会杀掉「纯粹由动量决定的」收益差**。
    ``_panels`` 那种恒定价格的面板里,任何选股都拿不出超额,只能验 null 的一侧,
    验不出「有边缘」的一侧,也就证不明这套对照有分辨力。这里让每 (票, 日) 的收益
    是独立噪声、动量是与噪声无关的固定抽样,选股能力才有地方体现。
    """
    codes = [f"s{i}" for i in range(n_codes)]
    dates = [f"2026-06-{i + 1:02d}" for i in range(n_days)]
    rng = random.Random(19)
    mom = {c: rng.gauss(5.0, 8.0) for c in codes}
    close = {d: {c: 100.0 * (1.0 + rng.gauss(0.0, 3.0) / 100.0) for c in codes} for d in dates}
    return Panels(
        open={d: dict.fromkeys(codes, 100.0) for d in dates},
        close=close,
        liquid={d: set(codes) for d in dates},
        mom20={d: dict(mom) for d in dates},
        dates=dates,
    )


def _verdict(cands: dict[str, dict], panels: Panels, horizon: int = 5) -> dict:
    rows = evaluate_daily(cands, panels, horizon, status="formal_l4")
    matched = summarize_group("matched", rows["matched"])
    controls = [summarize_group(f"c{s}", rows[f"control_{s}"]) for s in CONTROL_SEEDS]
    return {
        "gap": control_gap(matched, controls),
        "win_gap": win_control_gap(matched, controls),
        "matched": matched,
        "absolute": summarize_absolute(rows["absolute"]),
    }


class TestControlGapDiscriminates:
    """端到端守住「否证环节对候选好坏敏感」。

    原来的 ``TestControlGap`` 全是手喂 ``GroupStat`` 的超额值,不经 ``evaluate_daily``
    /``random_control_row``,所以随机组把候选收益当被测量这个 bug 一直没被发现——
    实测完美预知的候选(配对超额 +5.79pct, t=+35.6)与纯随机选票(-0.14pct)拿到同
    一句「不含选股信息」,扫候选收益 -5%~+20% 时 gap 恒为 +0.0000。这里必须走完整
    链路。
    """

    HORIZON = 5
    N_HITS = 20

    def _signal_days(self, panels: Panels) -> list[str]:
        return panels.dates[: -(self.HORIZON + 2)]

    def _foresight(self, panels: Panels) -> dict[str, dict]:
        """按实际卖出价选票——选股能力的上限。用 sell_ds 而非 buy_ds：后者跟持有
        期收益无关,选出来的边缘接近 0,那样探针自己就是错的。"""
        cands: dict[str, dict] = {}
        for ds in self._signal_days(panels):
            _buy_ds, sell_ds = panels.window(ds, self.HORIZON)
            ranked = sorted(panels.close[sell_ds], key=lambda c: -panels.close[sell_ds][c])
            hits = sorted(ranked[: self.N_HITS])
            cands[ds] = {"formal_l4": hits, "all": hits}
        return cands

    def _random_picks(self, panels: Panels) -> dict[str, dict]:
        cands: dict[str, dict] = {}
        rng = random.Random(4242)
        for ds in self._signal_days(panels):
            hits = sorted(rng.sample(sorted(panels.liquid[ds]), self.N_HITS))
            cands[ds] = {"formal_l4": hits, "all": hits}
        return cands

    def test_perfect_foresight_beats_the_random_control(self):
        panels = _noisy_panels()
        out = _verdict(self._foresight(panels), panels, self.HORIZON)
        assert out["matched"].excess_pct > 1.0, out["matched"].excess_pct
        assert out["gap"]["gap"] > out["gap"]["seed_spread"]
        assert "含独立选股信息" in out["gap"]["verdict"], out["gap"]

    def test_random_picks_stay_inside_the_random_control(self):
        panels = _noisy_panels()
        out = _verdict(self._random_picks(panels), panels, self.HORIZON)
        assert abs(out["matched"].excess_pct) < 1.0, out["matched"].excess_pct
        assert "不含选股信息" in out["gap"]["verdict"], out["gap"]

    def test_gap_tracks_candidate_performance(self):
        """gap 必须随候选收益单调变化。bug 版本下它恒为 +0.0000。"""
        panels = _noisy_panels()
        strong = _verdict(self._foresight(panels), panels, self.HORIZON)["gap"]
        weak = _verdict(self._random_picks(panels), panels, self.HORIZON)["gap"]
        assert strong["gap"] > weak["gap"] + 1.0, (strong["gap"], weak["gap"])

    def test_win_gap_discriminates_like_the_return_gap(self):
        """胜率的否证环节也必须对候选好坏敏感,不能借收益那一栏。

        与收益版同构:共用基准若填错(比如拿候选自己的胜率当对照),完美预知与纯随机
        会拿到同一句判定。
        """
        panels = _noisy_panels()
        strong = _verdict(self._foresight(panels), panels, self.HORIZON)
        weak = _verdict(self._random_picks(panels), panels, self.HORIZON)
        assert strong["matched"].stock_win_excess_pct > 10.0, strong["matched"].stock_win_excess_pct
        assert abs(weak["matched"].stock_win_excess_pct) < 10.0, weak["matched"].stock_win_excess_pct
        assert strong["win_gap"]["gap"] > weak["win_gap"]["gap"] + 5.0
        assert "含独立选股信息" in strong["win_gap"]["verdict"], strong["win_gap"]
        assert "不含选股信息" in weak["win_gap"]["verdict"], weak["win_gap"]

    def test_win_gap_reads_the_win_column_not_the_return_column(self):
        """守住 _gap_of 的 attr 分派:两个 gap 不能是同一个数。

        实现时 ``beats_band`` / ``_gap_verdict`` 两处曾仍读 ``matched.excess_pct``,
        那样胜率块会把收益的数字印出来,而表头写着胜率。
        """
        panels = _noisy_panels()
        out = _verdict(self._foresight(panels), panels, self.HORIZON)
        assert out["win_gap"]["matched_excess"] == pytest.approx(out["matched"].stock_win_excess_pct, abs=1e-4)
        assert out["win_gap"]["matched_excess"] != pytest.approx(out["gap"]["matched_excess"], abs=1e-4)


class TestEvaluateDaily:
    def _cands(self, panels: Panels, n_hits: int = 20, n_wide: int = 60) -> dict[str, dict]:
        codes = sorted(next(iter(panels.liquid.values())))
        return {
            d: {"formal_l4": codes[:n_hits], "all": codes[:n_wide]}
            for d in panels.dates[:-25]  # 留出足够的前向窗口
        }

    def test_matched_and_controls_share_the_same_baseline(self):
        """两组的 control 栏必须是同一条基准线（配对篮),超额才可直接相减比较。

        原先这里断言两组的 **net** 相同,那正是 bug 本身：随机组的被测量被填成了
        候选自己的收益,于是 gap 里候选项精确抵消,整个否证环节对候选好坏不敏感。
        共用的应该是基准(control),被测量(net)必须各是各的篮子。
        """
        panels = _panels(60)
        rows = evaluate_daily(self._cands(panels), panels, 5, status="formal_l4")
        assert rows["matched"], "配对组应有观测"
        for seed in CONTROL_SEEDS:
            ctrl = rows[f"control_{seed}"]
            assert ctrl
            by_date = {r["date"]: r for r in ctrl}
            for row in rows["matched"]:
                assert by_date[row["date"]]["control"] == pytest.approx(row["control"])

    def test_control_net_is_the_random_basket_not_the_candidates(self):
        """随机组的 net 必须是随机篮自己的收益。恒定行情下两者都等于 -成本,
        分不出来,所以给命中票加一个边缘再看：net 不应跟着候选动。"""
        panels = _panels(60)
        codes = sorted(next(iter(panels.liquid.values())))
        hits = codes[:20]
        for ds in panels.dates:
            closes = panels.close.get(ds) or {}
            for code in hits:
                if code in closes:
                    closes[code] = closes[code] * 1.5
        rows = evaluate_daily(self._cands(panels), panels, 5, status="formal_l4")
        matched_net = rows["matched"][0]["net"]
        ctrl_net = rows[f"control_{CONTROL_SEEDS[0]}"][0]["net"]
        assert matched_net > ctrl_net + 1.0, "候选被拉高后,随机组的 net 不应跟着涨"

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

    def test_absolute_covers_all_hits_not_the_paired_subset(self):
        """绝对口径算在全部候选上：配对会丢掉没有同动量对照的票,而漏斗当天
        真正给出的是全集,分母本就不同。"""
        panels = _panels(60)
        cands = self._cands(panels, n_hits=20, n_wide=25)
        rows = evaluate_daily(cands, panels, 5, status="l4_vs_rest")
        assert rows["absolute"]
        assert all(r["size_abs"] == 20 for r in rows["absolute"])
        assert max(r["size"] for r in rows["matched"]) < rows["absolute"][0]["size_abs"]

    def test_absolute_survives_days_where_pairing_fails(self):
        """配对不成的日子不能连绝对收益一起丢——所以它算在配对之前。"""
        panels = _panels(60)
        codes = sorted(next(iter(panels.liquid.values())))
        # 宽池 == 命中集 -> 无对照可配,配对组整段为空。
        cands = {d: {"formal_l4": codes[:20], "all": codes[:20]} for d in panels.dates[:-25]}
        rows = evaluate_daily(cands, panels, 5, status="l4_vs_rest")
        assert rows["matched"] == []
        assert len(rows["absolute"]) >= MIN_DAYS

    def test_absolute_net_is_cost_deducted_and_bench_is_not(self):
        """基准是「不动手」的参照,不产生交易,所以不扣成本。"""
        panels = _panels(60)
        panels.bench_open = dict.fromkeys(panels.dates, 100.0)
        panels.bench_close = dict.fromkeys(panels.dates, 100.0)
        rows = evaluate_daily(self._cands(panels), panels, 5, status="formal_l4")
        row = rows["absolute"][0]
        assert row["net_abs"] == pytest.approx(-ROUND_TRIP_COST_PCT)
        assert row["bench"] == pytest.approx(0.0)

    def test_absolute_bench_is_none_without_benchmark_panels(self):
        panels = _panels(60)
        rows = evaluate_daily(self._cands(panels), panels, 5, status="formal_l4")
        assert all(r["bench"] is None for r in rows["absolute"])


class TestControlGap:
    """随机负控制：漏斗的超额到底是选股信息,还是只是「站在了某个动量位置上」。

    修好抵消基准（见 ``random_control_row``）后实测 L4 vs 同动量非 L4 候选,71 个信号日
    2026-05-25~09-01：T+10 配对超额 +2.47pct、t=+1.48,随机控制 +0.54~+2.10,差距
    +1.414 小于种子宽度 1.555 —— 高过上界但幅度不够,三格都没到「含选股信息」。
    早先记的 +4.10pct/t=+3.55/在区间之下是抵消基准算出来的,已作废。

    本类每个用例守住 verdict 的一种分支：低于下界 / 区间内 / 高过上界但幅度薄 / 跑赢。
    三种否证理由不同,句式不能混用,否则报告会把数读反。
    """

    def _stat(self, excess: float | None) -> GroupStat:
        return GroupStat("x", 30, 13.7, None, None, excess, 3.55, 70.0, -0.16)

    def test_inside_control_range_has_no_selection_value(self):
        """落在种子区间里：分不出漏斗和「同动量随便挑」。取一格真的被包住的数。"""
        gap = control_gap(self._stat(4.600), [self._stat(e) for e in (4.477, 4.866, 5.165)])
        assert gap["beats_band"] is False
        assert "落在" in gap["verdict"]
        assert gap["control_excess_min"] == pytest.approx(4.477)
        assert gap["control_excess_max"] == pytest.approx(5.165)
        assert gap["gap"] < 0

    def test_below_every_seed_says_random_did_better(self):
        """低于所有种子要说「随便挑还更好」,不能沿用「落在区间内」的句式。

        4.098 在 4.477 之下,不在 [4.477, 5.165] 里。这两种都不构成证据,但把「比
        每个种子都差」印成「被区间包住」是把数读反了。
        """
        gap = control_gap(self._stat(4.098), [self._stat(e) for e in (4.477, 4.866, 5.165)])
        assert gap["beats_band"] is False
        assert "落在" not in gap["verdict"]
        assert "随便挑还更好" in gap["verdict"]
        assert gap["gap"] == pytest.approx(4.098 - 4.836, abs=1e-3)

    def test_above_the_band_but_within_spread_says_so_explicitly(self):
        """高于所有种子、但差距不超过种子宽度：不算跑赢,也**不能说「落在区间内」**。

        旧版对这一格印的是「落在随机负控制区间内」,那句话是错的——超额明明在上界
        之上。实测 T+10 就是这一格（+2.468 vs 种子上界 +2.095,宽度 1.555）,报告里
        照旧句式读会以为被区间包住了。两种情况都不构成证据,但理由不同。
        """
        gap = control_gap(self._stat(0.35), [self._stat(e) for e in (0.10, 0.30)])
        assert gap["gap"] == pytest.approx(0.15)
        assert gap["seed_spread"] == pytest.approx(0.20)
        assert gap["beats_band"] is True
        assert "落在" not in gap["verdict"]
        assert "证据不足" in gap["verdict"]

    def test_gap_equal_to_spread_is_not_a_win(self):
        """边界取 <=：刚好等于宽度不算跑赢。

        取二进制可精确表示的值（0.25/0.75/1.25）,否则 0.1/0.3/0.4 那组在浮点上
        gap 比 spread 大一个 eps,考的就不是这条分支了。
        """
        # 均值 0.5、宽度 0.5 -> matched=1.0 时 gap 恰好等于 spread。
        gap = control_gap(self._stat(1.0), [self._stat(e) for e in (0.25, 0.75)])
        assert gap["gap"] == pytest.approx(gap["seed_spread"])
        assert "证据不足" in gap["verdict"]

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
