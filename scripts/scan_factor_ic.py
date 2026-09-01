"""扫描生产在用指标的 IC：哪些真有预测力，哪些是噪声。

在改动八通道阈值之前跑它。生产漏斗全是阈值门（`rank(axis=1)` 在 core/ 零命中），
门槛线上的票本质随机、参数必然过拟合（walk-forward 1/16）、且只能过滤不能排序。
IC 用横截面秩相关衡量预测力，不需要切点。

因子池取自生产实际计算的字段（core/wyckoff_engine.py 与 core/layer2_strength.py
里出现的 ret3/5/20/60/120、rps_fast/slow、turnover、vol_ratio、bias、dry_vol 等），
而非凭空构造——避免测了一堆生产用不上的东西。

用法::

    # 全因子扫描，T+5 与 T+10
    python scripts/scan_factor_ic.py --cache /tmp/hist_2y.csv

    # 只看某几个因子
    python scripts/scan_factor_ic.py --factors dry_vol_q250,ret20,rps_slow

    # 滚动窗口验证（按 AGENTS.md 规则 7：全样本最优不算证据）
    python scripts/scan_factor_ic.py --walk-forward 4
"""

from __future__ import annotations

import argparse
import json
from functools import lru_cache
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from core.factor_ic import (
    MIN_CROSS_SECTION,
    FactorICResult,
    composite_weights,
    summarize_ic,
)

WARMUP = 260
QUANTILE_GROUPS = 5
# beta 估计窗口。截面回归剔除市场因子时用它算个股对等权市场的敏感度。
BETA_WINDOW = 120
# beta 估计所需最少有效样本，不足则该股当日不参与中性化后的 IC。
BETA_MIN_OBS = 60
# 行业内分位所需的最少同业成员数。低于此值组内 pct rank 只有寥寥几个取值，
# 1 只成员更是恒等于 1.0，是噪声而非信号。
WITHIN_SECTOR_MIN_MEMBERS = 5
# RPS 窗口必须与生产同源：core/wyckoff_engine.py 的 rps_window_fast / rps_window_slow。
RPS_WINDOW_FAST = 50
RPS_WINDOW_SLOW = 120


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="生产指标 IC 扫描")
    p.add_argument("--cache", default="", help="行情 CSV（含 ts_code,trade_date,open,close,high,low,vol,amount）")
    p.add_argument("--start", default="2024-08-01")
    p.add_argument("--horizons", default="5,10", help="前瞻期，逗号分隔")
    p.add_argument("--factors", default="", help="只测这些因子，逗号分隔；留空=全部")
    p.add_argument("--min-amount-wan", type=float, default=8000.0, help="流动性门槛，与生产 RISK_OFF 档一致")
    p.add_argument("--walk-forward", type=int, default=0, help="切成 N 段分别评估，检查跨段稳定性")
    p.add_argument(
        "--beta-neutral",
        action="store_true",
        help="前瞻收益先对滚动 beta 做截面回归取残差，分离市场敏感度与真 alpha",
    )
    p.add_argument("--json-out", default="")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--save-db", action="store_true", help="落库到 factor_ic_daily")
    return p.parse_args()


def amount_to_wan_divisor(frame: pd.DataFrame) -> float:
    """推断 amount 列的单位，返回「换算成万元要除以多少」。

    两个数据源的 amount 单位差 1000 倍，靠列名分不出来（都叫 amount）：
      - tushare 日线：vol 单位手、amount 单位**千元**，故 amount/(vol*close) ≈ 0.1
      - backtest 快照 hist_full.csv.gz：amount 单位**元**，故该比值 ≈ 100

    2026-09-01 修：此处原先硬编码 `/10`（按千元写的），而 CI 实际喂的是快照（元），
    于是 8000 万元的流动性门槛被稀释成约 8 万元，几乎全市场放行——截面宽 4349 而非
    2630。IC 结论未翻（ret60 T+10 由 -0.0659 变 -0.0722，反而更强），但样本域不是
    声称的那个，故按比值判定而不再假定来源。
    """
    probe = frame[["amount", "vol", "close"]].apply(pd.to_numeric, errors="coerce").dropna()
    probe = probe[(probe.vol > 0) & (probe.close > 0) & (probe.amount > 0)]
    if probe.empty:
        return 1e4
    ratio = float((probe.amount / (probe.vol * probe.close)).median())
    # 1.0 是两者的几何中点（0.1 与 100 各差 10 倍以上），足够把两种来源分开。
    return 10.0 if ratio < 1.0 else 1e4


def load_market(cache: str, start: str) -> pd.DataFrame:
    """读行情。兼容两种列名：tushare 原始（ts_code/trade_date/vol）与
    backtest 快照 hist_full.csv.gz（symbol/date/volume）——后者是 CI 里的实际来源。

    amount 一并归一到**万元**，让下游门槛不必再关心来源单位。"""
    if not cache or not Path(cache).exists():
        raise SystemExit("请用 --cache 指定行情 CSV（可复用 backtest 快照的 hist_full）")
    frame = pd.read_csv(cache, dtype={"ts_code": str, "symbol": str, "trade_date": str}, low_memory=False)
    if "ts_code" not in frame.columns and "symbol" in frame.columns:
        frame = frame.rename(columns={"symbol": "ts_code"})
    if "vol" not in frame.columns and "volume" in frame.columns:
        frame = frame.rename(columns={"volume": "vol"})
    if "trade_date" in frame.columns:
        frame["d"] = pd.to_datetime(frame.trade_date, format="%Y%m%d", errors="coerce")
    else:
        frame["d"] = pd.to_datetime(frame["date"], errors="coerce")
    missing = {"ts_code", "open", "close", "high", "low", "vol", "amount"} - set(frame.columns)
    if missing:
        raise SystemExit(f"行情缺少必要列: {sorted(missing)}")
    frame = frame.dropna(subset=["d"])
    frame = frame[frame.d >= pd.Timestamp(start)]
    frame["amount"] = pd.to_numeric(frame.amount, errors="coerce") / amount_to_wan_divisor(frame)
    return frame.sort_values(["ts_code", "d"])


def build_factors(market: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    """构造与生产同名的因子面板。

    所有因子只用 T 日及之前的数据，前瞻收益用 T+1 开盘买入，避免用到未来信息。
    """
    piv = {
        c: market.pivot_table(index="d", columns="ts_code", values=c)
        for c in ("close", "open", "high", "low", "vol", "amount")
    }
    close, open_, high, low, vol, amount = (piv[k] for k in ("close", "open", "high", "low", "vol", "amount"))

    factors: dict[str, pd.DataFrame] = {}
    # 动量族：生产的 ret3/5/20/60/120
    for win in (3, 5, 20, 60, 120):
        factors[f"ret{win}"] = close.pct_change(win, fill_method=None) * 100
    # 相对强弱：窗口必须跟生产一致。生产是 RPS50 / RPS120 的**全市场**分位
    # （core/wyckoff_engine.py rps_window_fast=50 / rps_window_slow=120，
    # 分位在 core/layer2_strength.py:105-106 用 rank(pct=True)），
    # 2026-09-01 前这里写的是 20/60 却在注释里自称「同构」：实测两套 slow 的
    # 日均截面秩相关只有 +0.63，等于在测另一个因子，故改回 50/120。
    for label, win in (("rps_fast", RPS_WINDOW_FAST), ("rps_slow", RPS_WINDOW_SLOW)):
        factors[label] = (close.pct_change(win, fill_method=None) * 100).rank(axis=1, pct=True) * 100
    # 行业内分位是**另一个**因子，不是 rps 的等价写法，故单独命名。
    # 注意：全市场分位对 Rank IC 是恒等变换——Spearman 对单调变换不变，故
    # `rank(ret60)` 与 `ret60` 的 Rank IC 逐位相同（2026-08-30 实测差 <1e-9），
    # 所以 rps_slow 相对 ret60 不携带新信息，只是与生产同名便于对照；
    # 真正带新信息的是行业内重排：同一 ret20 在弱行业里是龙头、在强行业里是垫底。
    sector_map_for_rps = _load_sector_map()
    if sector_map_for_rps:
        for label, win in (("sector_rel_20", 20), ("sector_rel_60", 60)):
            mom = close.pct_change(win, fill_method=None) * 100
            factors[label] = _within_sector_rank(mom, sector_map_for_rps)
    # 量能族：dry_vol 生产用 250 日分位（PR 早前把默认从 0.05 放宽到 0.20）
    v20 = vol.rolling(20).mean()
    factors["dry_vol_q250"] = v20.rolling(250).rank(pct=True) * 100
    factors["vol_ratio"] = vol / v20
    factors["turnover_amt"] = amount.rolling(20).mean()
    # 位阶与乖离：生产的 bias_200、位阶保护
    ma200 = close.rolling(200).mean()
    factors["bias_200"] = (close / ma200 - 1) * 100
    ma20 = close.rolling(20).mean()
    factors["bias_20"] = (close / ma20 - 1) * 100
    lo250 = low.rolling(250).min()
    factors["price_from_low250"] = (close / lo250 - 1) * 100
    hi60 = high.rolling(60).max()
    factors["dist_to_high60"] = (close / hi60 - 1) * 100
    # 波动与形态
    factors["amplitude60"] = (hi60 / low.rolling(60).min() - 1) * 100
    rng = high - low
    factors["close_position"] = ((close - low) / rng.where(rng > 0)) * 100

    _add_volume_school_factors(factors, close, vol)

    # --- 板块强度因子 ---
    # 2026-08-26 实测（169 个交易日、日均 3823 只、扣 0.202%），这是当前唯一测出**正 IC**
    # 的维度，其余因子（ret60/vol_ratio/量学 5 个）皆为负或近零：
    #
    #   静态行业 tushare industry（110 个）  IC +0.0330  IC_IR +0.19  IC为正日 57%
    #                                       强板块top20% 净超额 +0.37pct  为正日 58%
    #   动态概念 东财（340 个，成员>=15）     IC +0.0343  IC_IR +0.19  IC为正日 54%
    #                                       强板块top20% 净超额 +0.49pct  为正日 53%
    #
    # 一个反直觉的发现：**静态行业标签并不比动态概念差**（IC 几乎相同，为正日反而更高）。
    # 原因是强度本身是动态的——标签静态，但「成员股 5 日涨幅中位数」每天在变，
    # 只要标签把相关股票分到一组就能捕捉轮动。故「静态分类跟不上题材切换」的担心不成立。
    #
    # 2026-08-26 补测（444 日，2024-06~2026-08，含 2024 下半年）：**只有 5 日窗口有效**——
    #   sector_strength_5d   T+5   IC +0.0420  IC_IR +0.24  为正日 58%
    #   sector_strength_5d   T+10  IC +0.0149  IC_IR +0.08  为正日 58%
    #   sector_strength_20d  T+5   IC +0.0036  IC_IR +0.02  为正日 54%（无方向性）
    #   sector_strength_20d  T+10  IC -0.0104  IC_IR -0.06  为正日 52%（无方向性）
    # 即板块轮动确实快：20 日窗口已无信息，做组合打分时板块维度只能取短窗口。
    #
    # 同一区间上 ret60（IR -0.37 -> -0.14）与 dry_vol_q250（IR -0.35 -> -0.16）均大幅衰减，
    # 说明这两个因子对区间敏感——此前「三段方向全一致」是在 2024-08 之后的样本上得出的。
    # 影子池正用这两个因子，故其预期效果应下调；这与 2026-08-24 实测
    # （影子池 T+1 净超额 -0.91pct）方向吻合。
    #
    # IC_IR 均未过 0.30 门槛。故先入 scanner 持续跟踪，暂不据此改题材层。
    # 注意：这里测的是「全市场排序」效果，而生产题材层作用于已过结构通道的小候选集，
    # 两者不等价——改动前需在候选集口径上单独验证。
    sector_map = _load_sector_map()
    if sector_map:
        factors["sector_strength_5d"] = _sector_strength(close, sector_map, 5)
        factors["sector_strength_20d"] = _sector_strength(close, sector_map, 20)
    return factors, close, open_


def _add_volume_school_factors(factors: dict[str, pd.DataFrame], close: pd.DataFrame, vol: pd.DataFrame) -> None:
    """量学候选因子。原本内联在 build_factors 里，抽出以满足函数长度上限。"""
    # --- 量学（A 股本土量价流派）候选因子 ---
    # 量学与威科夫同源：都主张「量在价先、跟随大资金意图」，只是量学更贴 A 股微观结构
    # （涨停板制度、散户主导、题材轮动）。这里把它的核心量柱形态做成因子持续观测。
    #
    # 2026-08-25 首轮实测（498 个交易日，3 段各约 75 日）：**无一通过可用门槛**——
    #   vs_multiple_vol（倍量柱）      IC -0.0038  近乎零
    #   vs_golden_vol（黄金柱）        IC +0.0051  近乎零
    #   vs_volume_pit（量坑）          方向仅 2/3 段一致
    #   vs_vol_stair（阶梯量）         方向仅 2/3 段一致
    #   vs_price_vol_divergence       IC +0.0327 但样本仅 73 天，且拆开四象限后
    #                                 「涨+缩量」净超额 -0.08、为正日 48%（无方向性），
    #                                 即它是排序器而非选股器，不可据此建通道
    # 故只进 scanner 观测、不进生产形态。若某个因子日后转为三段一致且过门槛，再评估。
    v5 = vol.rolling(5).mean()
    v10 = vol.rolling(10).mean()
    prev_vol = vol.shift(1)
    # 倍量柱：当日量 / 前日量。量学视 >=2 为主力进场。
    factors["vs_multiple_vol"] = vol / prev_vol.where(prev_vol > 0)
    # 黄金柱：当日量相对 5 日与 10 日均量中较大者的倍数。
    golden_base = v5.where(v5 > 0).clip(lower=v10.where(v10 > 0))
    factors["vs_golden_vol"] = vol / golden_base
    # 量坑：当日量在近 20 日中的分位，越低越"坑"。
    factors["vs_volume_pit"] = vol.rolling(20).rank(pct=True) * 100
    # 阶梯量：5 日均量相对 10 日均量的抬升幅度，衡量量能是否逐级放大。
    factors["vs_vol_stair"] = (v5 / v10.where(v10 > 0) - 1) * 100
    # 价量背离：5 日涨幅 × 量能收缩程度。量学认为"涨而缩量"是主力控盘特征。
    # 注意该构造把「涨+缩量」与「跌+放量」混为同号，判读时须拆象限看。
    v5_prev = v5.shift(5)
    factors["vs_price_vol_divergence"] = (close.pct_change(5, fill_method=None) * 100) * (
        1 - v5 / v5_prev.where(v5_prev > 0)
    )


@lru_cache(maxsize=1)
def _load_sector_map() -> dict[str, str]:
    """读生产同一份行业映射（tushare stock_basic 的 industry 字段，24h 缓存）。

    缺失时返回空 dict——scanner 少两个因子而非整体失败。
    一次运行内被 rps 与 sector_strength 两处调用，故加进程内缓存。
    """
    import json

    try:
        from integrations.market_metadata import SECTOR_CACHE

        if not SECTOR_CACHE.exists():
            return {}
        data = json.loads(SECTOR_CACHE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _within_sector_rank(panel: pd.DataFrame, sector_map: dict[str, str]) -> pd.DataFrame:
    """把面板值换成**行业内**百分位（0~100），行业外的股票留 NaN。

    与全市场 rank 的区别：全市场 rank 是原值的单调变换，对 Rank IC 无影响；
    行业内 rank 会按行业重排秩序，故携带新信息。
    """
    codes = [c for c in panel.columns if c in sector_map]
    if len(codes) < 50:
        return pd.DataFrame(index=panel.index, columns=panel.columns, dtype=float)
    labels = pd.Series({c: sector_map[c] for c in codes})
    # 小行业剔除：pct rank 在只有 1 个成员的组里恒为 1.0，会把这些票当成永久的
    # 行业龙头喂进 IC；组内 2~3 只也只能取到 0.33/0.5/1.0 这几个值，秩噪声大于信号。
    sizes = labels.value_counts()
    keep = set(sizes[sizes >= WITHIN_SECTOR_MIN_MEMBERS].index)
    labels = labels[labels.isin(keep)]
    if len(labels) < 50:
        return pd.DataFrame(index=panel.index, columns=panel.columns, dtype=float)
    sub = panel[list(labels.index)]
    # 沿列方向按行业分组，组内求百分位；transform 保持原形状。
    ranked = sub.T.groupby(labels).rank(pct=True).T * 100
    return ranked.reindex(columns=panel.columns)


def _sector_strength(close: pd.DataFrame, sector_map: dict[str, str], window: int) -> pd.DataFrame:
    """个股所属板块的相对强度：同板块成员 window 日涨幅的中位数。

    用中位数而非均值：单只涨停股不应把整个板块拉高。
    结果按行（截面日）广播回个股，故每只股票拿到的是「它所在板块的强度」。
    """
    codes = [c for c in close.columns if c in sector_map]
    if len(codes) < 50:
        return pd.DataFrame(index=close.index, columns=close.columns, dtype=float)
    labels = pd.Series({c: sector_map[c] for c in codes})
    rets = close[codes].pct_change(window, fill_method=None) * 100
    # groupby 沿列方向聚合，再 reindex 回原列顺序。
    grouped = rets.T.groupby(labels).median().T
    return grouped.reindex(columns=labels.values).set_axis(codes, axis=1).reindex(columns=close.columns)


def rolling_beta(close: pd.DataFrame, window: int = BETA_WINDOW) -> pd.DataFrame:
    """个股对等权市场的滚动 beta，只用 T 日及之前的数据。

    等权市场收益取当日横截面均值——它与 000001 的加权口径不同，但 IC 是截面统计量，
    需要的是「同一截面内谁更敏感」的排序，等权基准足够且不引入外部数据依赖。
    """
    ret = close.pct_change(fill_method=None)
    mkt = ret.mean(axis=1)
    # cov/var 用滚动窗口逐列算；min_periods 保证窗口早期不出伪 beta。
    cov = ret.rolling(window, min_periods=BETA_MIN_OBS).cov(mkt)
    var = mkt.rolling(window, min_periods=BETA_MIN_OBS).var()
    return cov.div(var, axis=0)


def _beta_neutral(fwd: pd.Series, beta: pd.Series) -> pd.Series:
    """截面上把收益对 beta 回归，返回残差。

    减截面均值对 rank IC 是**恒等变换**（秩不变，IC 逐位相同），故必须做回归而非去均值：
    只有残差化才会改变个股间的秩序，从而分离「因子选到高 beta 票」与「因子真有 alpha」。
    """
    frame = pd.DataFrame({"r": fwd, "b": beta}).dropna()
    if len(frame) < MIN_CROSS_SECTION or frame.b.nunique() < 2:
        return pd.Series(dtype=float)
    var = float(frame.b.var())
    if not var or var != var:
        return pd.Series(dtype=float)
    slope = float(frame.b.cov(frame.r)) / var
    return frame.r - (frame.b - float(frame.b.mean())) * slope


def _daily_ic(fac: pd.Series, fwd: pd.Series, mask: pd.Series) -> tuple[float | None, float | None]:
    """单日 Rank IC 与分位单调性。"""
    frame = pd.DataFrame({"f": fac, "r": fwd})[mask].dropna()
    if len(frame) < MIN_CROSS_SECTION or frame.f.nunique() < QUANTILE_GROUPS:
        return None, None
    # 秩相关 = 对秩取 Pearson，与 Spearman 等价；避免引入 scipy 依赖。
    ic = frame.f.rank().corr(frame.r.rank())
    try:
        groups = pd.qcut(frame.f.rank(method="first"), QUANTILE_GROUPS, labels=False)
        means = frame.groupby(groups).r.mean()
        mono = means.rank().corr(pd.Series(means.index, index=means.index).rank()) if len(means) >= 3 else None
    except ValueError:
        mono = None
    return (None if ic is None or ic != ic else float(ic)), (None if mono is None or mono != mono else float(mono))


def evaluate(
    factors: dict[str, pd.DataFrame],
    close: pd.DataFrame,
    open_: pd.DataFrame,
    horizons: tuple[int, ...],
    min_amount_wan: float,
    liquidity_wan: pd.DataFrame,
    date_slice: tuple[int, int] | None = None,
    beta: pd.DataFrame | None = None,
) -> list[FactorICResult]:
    dates = list(close.index)
    lo, hi = date_slice or (WARMUP, len(dates) - max(horizons) - 2)
    # 流动性面板单独传入：--factors 过滤后 turnover_amt 可能不在 factors 里。
    amt20 = liquidity_wan
    out: list[FactorICResult] = []
    for name, panel in factors.items():
        for horizon in horizons:
            ics: list[float] = []
            monos: list[float] = []
            widths: list[float] = []
            for i in range(lo, hi):
                exit_idx = i + 1 + horizon
                if exit_idx >= len(dates):
                    break
                entry = open_.iloc[i + 1]
                fwd = (close.iloc[exit_idx] / entry - 1) * 100
                liquid = (amt20.iloc[i] >= min_amount_wan) & entry.notna() & (entry > 0)
                if beta is not None:
                    fwd = _beta_neutral(fwd[liquid], beta.iloc[i][liquid])
                    if fwd.empty:
                        continue
                    # beta 缺失的票已被残差化丢掉，mask 必须跟着收窄，
                    # 否则 avg_universe 记的是中性化前的宽度。
                    liquid = liquid & liquid.index.isin(fwd.index)
                ic, mono = _daily_ic(panel.iloc[i], fwd, liquid)
                if ic is None:
                    continue
                ics.append(ic)
                widths.append(float(liquid.sum()))
                if mono is not None:
                    monos.append(mono)
            out.append(summarize_ic(name, horizon, ics, widths, monos))
    return out


def render(results: list[FactorICResult], title: str) -> str:
    rows = sorted(results, key=lambda r: -(abs(r.ic_ir) if r.ic_ir is not None else 0))
    lines = [
        f"**{title}**",
        "",
        "| 因子 | 前瞻 | 天数 | 截面宽 | Rank IC | IC_IR | 为正% | 单调性 | 判定 |",
        "| --- | --: | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for r in rows:
        if r.rank_ic is None:
            lines.append(f"| {r.name} | T+{r.horizon} | {r.days} | — | — | — | — | — | {r.verdict} |")
            continue
        ir = f"{r.ic_ir:+.2f}" if r.ic_ir is not None else "—"
        mono = f"{r.monotonicity:+.2f}" if r.monotonicity is not None else "—"
        lines.append(
            f"| {r.name} | T+{r.horizon} | {r.days} | {r.avg_universe:.0f} | {r.rank_ic:+.4f} | "
            f"{ir} | {r.positive_ratio:.0f}% | {mono} | {r.verdict} |"
        )
    usable = [r for r in results if r.useful]
    lines += ["", f"可用因子 {len(usable)} / {len(results)}"]
    weights = composite_weights(results)
    if weights:
        lines += ["", "**建议合成权重**（按 |IC_IR|，负号表示反向使用）", ""]
        for name, weight in sorted(weights.items(), key=lambda kv: -abs(kv[1])):
            lines.append(f"- `{name}` {weight:+.4f}")
    else:
        lines += ["", "无因子通过可用门槛（|IC|>=0.02 且 |IC_IR|>=0.30 且非无方向性）。"]
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    market = load_market(args.cache, args.start)
    print(f"[ic] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    factors, close, open_ = build_factors(market)
    # 在 --factors 过滤之前留存流动性面板，否则过滤掉 turnover_amt 会导致门槛无法计算。
    # amount 已在 load_market 里归一到万元，此处不再换算。
    liquidity = factors["turnover_amt"]
    if args.factors:
        keep = {x.strip() for x in args.factors.split(",") if x.strip()}
        missing = keep - set(factors)
        if missing:
            raise SystemExit(f"未知因子: {sorted(missing)}；可选 {sorted(factors)}")
        factors = {k: v for k, v in factors.items() if k in keep}
    print(f"[ic] 因子 {len(factors)} 个 × 前瞻 {horizons}")

    beta = None
    if args.beta_neutral:
        beta = rolling_beta(close)
        print(f"[ic] beta 中性化开启（窗口 {BETA_WINDOW} 日，最少 {BETA_MIN_OBS} 个观测）")
    suffix = "（beta 中性）" if beta is not None else ""

    # 落库的 segment 加后缀：唯一键是 (eval_date, factor_name, horizon, segment)，
    # 同日跑原始+beta 中性两次会互相 upsert 覆盖。加后缀后两份并存，
    # 且既有消费方查 segment='full' 不受诊断性运行影响。
    seg_tag = "_bn" if beta is not None else ""

    payload: dict[str, object] = {"beta_neutral": bool(args.beta_neutral)}
    sections: list[str] = []
    full = evaluate(factors, close, open_, horizons, args.min_amount_wan, liquidity, beta=beta)
    text = render(full, f"因子 IC 全样本扫描{suffix}")
    print("\n" + text)
    sections.append(text)
    full_rows = [r.as_dict() for r in full]
    payload["full"] = full_rows
    dates = list(close.index)
    win = (str(dates[WARMUP].date()), str(dates[-1].date()))
    # 显式记录 IC 实际覆盖的区间。WARMUP 会把行情起点往后推 260 个交易日，
    # 光看 --start 会误判样本范围（2026-08-30 就因此把 2024 下半年当成已覆盖）。
    payload["ic_window"] = {"market_start": str(dates[0].date()), "ic_start": win[0], "ic_end": win[1]}
    print(f"[ic] 行情自 {dates[0].date()} 起；扣掉 WARMUP={WARMUP} 后 IC 实际覆盖 {win[0]} ~ {win[1]}")
    if args.save_db:
        _save(full_rows, f"full{seg_tag}", win, composite_weights(full))

    if args.walk_forward > 1:
        lo, hi = WARMUP, len(dates) - max(horizons) - 2
        step = (hi - lo) // args.walk_forward
        segments = []
        for k in range(args.walk_forward):
            s0, s1 = lo + k * step, (lo + (k + 1) * step) if k < args.walk_forward - 1 else hi
            seg = evaluate(factors, close, open_, horizons, args.min_amount_wan, liquidity, (s0, s1), beta=beta)
            seg_rows = [r.as_dict() for r in seg]
            segments.append(seg_rows)
            label = f"{dates[s0].date()}~{dates[s1 - 1].date()}"
            if args.save_db:
                _save(
                    seg_rows,
                    f"seg{k + 1}{seg_tag}",
                    (str(dates[s0].date()), str(dates[s1 - 1].date())),
                    composite_weights(seg),
                )
            text = render(seg, f"分段 {k + 1}/{args.walk_forward}　{label}{suffix}")
            print("\n" + text)
            sections.append(text)
        payload["segments"] = segments
        stable = _stability(payload)
        print("\n" + stable)
        sections.append(stable)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[ic] 已写 {args.json_out}")
    if not args.no_notify:
        _notify(sections)
    return 0


def _stability(payload: dict[str, object]) -> str:
    """跨段方向一致性——这才是能不能用的关键，而非全样本 IC 高低。"""
    segments = payload.get("segments") or []
    if not segments:
        return ""
    keys = {(r["name"], r["horizon"]) for r in segments[0]}
    lines = ["**跨段稳定性**", "", "| 因子 | 前瞻 | 方向一致 | 各段 IC |", "| --- | --: | --: | --- |"]
    for name, horizon in sorted(keys):
        vals = []
        for seg in segments:
            hit = next((r for r in seg if r["name"] == name and r["horizon"] == horizon), None)
            vals.append(hit.get("rank_ic") if hit else None)
        clean = [v for v in vals if v is not None]
        if len(clean) < 2:
            continue
        same = sum(1 for v in clean if v > 0)
        consistent = max(same, len(clean) - same)
        lines.append(
            f"| {name} | T+{horizon} | {consistent}/{len(clean)} | " + " ".join(f"{v:+.3f}" for v in clean) + " |"
        )
    lines += ["", "方向一致数 = 各段 IC 同号的最大计数。全段同号才说明因子稳定，否则属拟合。"]
    return "\n".join(lines)


def _save(rows: list[dict], segment: str, window: tuple[str, str], weights: dict[str, float]) -> None:
    from integrations.supabase_factor_ic import save_factor_ic_rows

    written = save_factor_ic_rows(rows, segment=segment, window_start=window[0], window_end=window[1], weights=weights)
    print(f"[ic] 落库 {segment}: {written}/{len(rows)} 行")


def _notify(sections: list[str]) -> None:
    import os

    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[ic] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"因子 IC 扫描｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    ok = send_feishu_notification(webhook, title, "\n\n---\n\n".join(s for s in sections if s))
    print("[ic] feishu sent" if ok else "[ic] feishu failed")


if __name__ == "__main__":
    _ = np  # numpy 由 pandas 间接需要，显式引用避免 lint 误删
    raise SystemExit(main())
