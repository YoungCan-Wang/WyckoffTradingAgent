"""Tests for the factor IC scanner's neutralization and within-sector ranking.

这些用例守的是两个曾经把噪声读成信号的坑：

1. **去均值对 Rank IC 是恒等变换**。要分离「因子只是选到了高 beta 票」和「因子真有
   alpha」，必须做截面回归取残差；减掉截面均值不改变任何一只票的秩，IC 逐位相同。
2. **全市场 rank 也是恒等变换**。`rank(ret20)` 与 `ret20` 的 Rank IC 完全相同，
   早前版本把它当独立因子，导致因子表虚增证据、BH 多重比较的检验数偏大。行业内分位
   才会重排秩序、携带原始动量之外的信息。

两条都是「看起来像新东西、数学上什么也没变」，靠肉眼审阅抓不住，只能用例守。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.scan_factor_ic import (
    BETA_MIN_OBS,
    RPS_WINDOW_FAST,
    RPS_WINDOW_SLOW,
    WITHIN_SECTOR_MIN_MEMBERS,
    _beta_neutral,
    _within_sector_rank,
    amount_to_wan_divisor,
    rolling_beta,
)


def _rank_ic(a: pd.Series, b: pd.Series) -> float:
    """Spearman 秩相关，与 scanner 的 _daily_ic 同口径。"""
    return float(a.rank().corr(b.rank()))


def _codes(n: int) -> list[str]:
    return [f"{i:06d}.SZ" for i in range(n)]


def _beta_driven_cross_section(n: int = 400, seed: int = 7):
    """造一个「因子只是在赌 beta」的截面：因子与 beta 强相关，收益完全由 beta 驱动。"""
    rng = np.random.default_rng(seed)
    codes = _codes(n)
    beta = pd.Series(rng.normal(1.0, 0.35, n), index=codes)
    factor = pd.Series(beta.to_numpy() * 2 + rng.normal(0, 0.5, n), index=codes)
    fwd = pd.Series(beta.to_numpy() * 3.0 + rng.normal(0, 2.0, n), index=codes)
    return factor, fwd, beta


class TestBetaNeutral:
    def test_demeaning_would_not_change_rank_ic(self):
        """反证:减截面均值是恒等变换,所以中性化必须走回归而不是去均值。"""
        factor, fwd, _ = _beta_driven_cross_section()
        assert _rank_ic(factor, fwd) == _rank_ic(factor, fwd - fwd.mean())

    def test_residualizing_kills_a_pure_beta_factor(self):
        """纯 beta 因子中性化后 IC 应塌回噪声带——这正是这个开关要抓的假阳性。"""
        factor, fwd, beta = _beta_driven_cross_section()
        raw = _rank_ic(factor, fwd)
        resid = _beta_neutral(fwd, beta)
        neutral = _rank_ic(factor.loc[resid.index], resid)
        assert raw > 0.30
        assert abs(neutral) < 0.05

    def test_residual_is_orthogonal_to_beta(self):
        _, fwd, beta = _beta_driven_cross_section()
        resid = _beta_neutral(fwd, beta)
        assert abs(_rank_ic(resid, beta.loc[resid.index])) < 0.05

    def test_returns_empty_below_min_cross_section(self):
        """截面太窄就不出数,不能拿几十只票的残差当 IC。"""
        codes = _codes(40)
        fwd = pd.Series(np.arange(40.0), index=codes)
        beta = pd.Series(np.linspace(0.5, 1.5, 40), index=codes)
        assert _beta_neutral(fwd, beta).empty

    def test_returns_empty_when_beta_is_degenerate(self):
        """所有票同一个 beta 时回归无解,返回空而不是除零。"""
        codes = _codes(200)
        fwd = pd.Series(np.arange(200.0), index=codes)
        beta = pd.Series(np.ones(200), index=codes)
        assert _beta_neutral(fwd, beta).empty


class TestWithinSectorRank:
    def _panel(self, n: int = 400, seed: int = 11):
        rng = np.random.default_rng(seed)
        return pd.DataFrame(
            rng.normal(0, 5, (5, n)),
            index=pd.bdate_range("2024-01-01", periods=5),
            columns=_codes(n),
        )

    def test_global_rank_is_an_identity_transform(self):
        """反证:全市场分位与原值秩逐位相同,当独立因子等于重复计权。

        直接比秩而不是比相关系数——相关系数会带浮点残差(实测 0.9999999999999999),
        比秩是精确的,也更贴近「Rank IC 逐位相同」这个真正的论断。
        """
        panel = self._panel()
        row = panel.iloc[2]
        assert (row.rank(pct=True) * 100).rank().equals(row.rank())

    def test_within_sector_rank_reorders(self):
        """行业内分位会打乱全市场秩序,故携带原始动量之外的信息。"""
        panel = self._panel()
        sector_map = {code: f"S{i % 12}" for i, code in enumerate(panel.columns)}
        ranked = _within_sector_rank(panel, sector_map)
        assert _rank_ic(ranked.iloc[2], panel.iloc[2]) < 0.99

    def test_drops_sectors_below_min_members(self):
        """单成员行业的 pct rank 恒等于 1.0,会被当成永久龙头,必须剔除。"""
        panel = self._panel()
        codes = list(panel.columns)
        tiny = 30
        sector_map = {code: (f"solo{i}" if i < tiny else "BIG") for i, code in enumerate(codes)}
        ranked = _within_sector_rank(panel, sector_map)
        assert WITHIN_SECTOR_MIN_MEMBERS > 1
        assert int(ranked.iloc[2].notna().sum()) == len(codes) - tiny
        assert ranked.iloc[2][codes[:tiny]].isna().all()

    def test_returns_all_nan_when_coverage_too_thin(self):
        """行业映射覆盖不足时整列留空,scanner 少两个因子而非拿残缺截面出数。"""
        panel = self._panel()
        sector_map = {code: "BIG" for code in list(panel.columns)[:20]}
        assert _within_sector_rank(panel, sector_map).isna().all().all()

    def test_keeps_original_columns(self):
        """输出必须对齐原始列,否则 evaluate 里的 mask 会错位。"""
        panel = self._panel()
        sector_map = {code: f"S{i % 12}" for i, code in enumerate(panel.columns)}
        assert list(_within_sector_rank(panel, sector_map).columns) == list(panel.columns)


class TestRollingBeta:
    def _close(self, rows: int = 200, cols: int = 20, seed: int = 3) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        path = 100 * np.cumprod(1 + rng.normal(0, 0.02, (rows, cols)), axis=0)
        return pd.DataFrame(path, index=pd.bdate_range("2024-01-01", periods=rows), columns=_codes(cols))

    def test_no_beta_before_min_obs(self):
        """窗口早期不出伪 beta,否则中性化会拿几个点估的斜率去残差化。"""
        beta = rolling_beta(self._close())
        assert beta.iloc[BETA_MIN_OBS - 10].isna().all()

    def test_beta_averages_to_one_against_equal_weight_market(self):
        """基准是等权市场,故截面 beta 均值应≈1——错了说明基准算歪了。"""
        beta = rolling_beta(self._close())
        assert abs(float(beta.iloc[150].mean()) - 1.0) < 0.05

    def test_shape_matches_input(self):
        close = self._close()
        assert rolling_beta(close).shape == close.shape


class TestAmountUnit:
    """amount 单位判定。两个数据源都叫 amount 但差 1000 倍,靠列名分不出来。

    2026-09-01 前 main 上硬编码 `/10`（按 tushare 的千元写),而 CI 实际喂的是
    backtest 快照(元),于是 8000 万元的流动性门槛被稀释成约 8 万元,截面宽 4349
    而非 2630。IC 结论没翻,但样本域不是声称的那个,靠肉眼复核发现不了。
    """

    def _frame(self, *, amount_scale: float, n: int = 500, seed: int = 11) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        close = rng.uniform(5, 50, n)
        vol = rng.uniform(1e4, 1e6, n)  # 单位:手
        # 成交额的真值 = 手 * 100股 * 价,再按目标单位缩放
        return pd.DataFrame({"close": close, "vol": vol, "amount": vol * 100 * close * amount_scale})

    def test_detects_tushare_thousand_yuan(self):
        """tushare: amount 千元 -> 除以 10 得万元。"""
        assert amount_to_wan_divisor(self._frame(amount_scale=1e-3)) == 10.0

    def test_detects_snapshot_yuan(self):
        """快照 hist_full.csv.gz: amount 元 -> 除以 1e4 得万元。"""
        assert amount_to_wan_divisor(self._frame(amount_scale=1.0)) == 1e4

    def test_two_sources_differ_by_exactly_one_thousand(self):
        """反证:两种判定必须差 1000 倍,否则不是「单位换算」而是引入了新偏差。"""
        yuan = amount_to_wan_divisor(self._frame(amount_scale=1.0))
        thousand = amount_to_wan_divisor(self._frame(amount_scale=1e-3))
        assert yuan / thousand == 1000.0

    def test_falls_back_when_unusable(self):
        """全是缺失/零时不能崩,退回快照口径(CI 的实际来源)。"""
        frame = pd.DataFrame({"close": [0.0, np.nan], "vol": [0.0, 1.0], "amount": [np.nan, 0.0]})
        assert amount_to_wan_divisor(frame) == 1e4


class TestRpsWindowsMatchProduction:
    """RPS 窗口必须与生产同源,否则扫描表测的是另一个因子。

    2026-09-01 前这里是 20/60 却在注释里自称与生产「同构」;生产是 RPS50/RPS120
    (core/wyckoff_engine.py)。实测两套 slow 的日均截面秩相关只有 +0.63——
    不是换名,是换了因子。
    """

    def test_windows_equal_production_config(self):
        from core.wyckoff_engine import FunnelConfig

        cfg = FunnelConfig()
        assert (RPS_WINDOW_FAST, RPS_WINDOW_SLOW) == (cfg.rps_window_fast, cfg.rps_window_slow)
