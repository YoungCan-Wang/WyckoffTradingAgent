"""IC 反向打分影子池：把阈值门换成横截面排序的验证载体。

## 为什么不直接改八通道

2026-08-22 的 IC 扫描显示 32 个因子-前瞻组合**全为负 IC**，其中 19 个在 3 段样本上
方向全一致——生产四条通道（主升 rps_slow>=75、趋势延续 ret60 高、加速突破
ret20>=15% 且放量、点火破局放量破新高）方向都反了。

但 IC 只说明**方向**反了，不说明该设什么阈值；而阈值化本身就是过拟合来源
（同期参数网格 walk-forward 仅 1/16 个窗口为正）。所以正确做法是先并行跑一个
按 IC 加权排序的影子池，只写 observation 不下单，观察两三周再谈替换。

## 打分方式

对每个因子做**横截面分位**（0~100），按 `sign * |IC_IR|` 归一化权重加权求和。
反向因子（IC<0）取负号后参与，故最终分数越高越好。

与阈值门的关键差别：`RPS 69.3` 与 `RPS 75` 只是分数略低，不再是进/不进的鸿沟——
这消除了「门槛线上的票本质随机」这个问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 影子池在 signal_observations 里的标识。source 已有 shadow_added/shadow_removed
# 等用法，这里沿用同一列避免新建表。
SHADOW_SOURCE = "ic_shadow"
SHADOW_CHANNEL = "IC反向打分"
# 每日写入上限。取 top-N 而非全部：影子池的目的是模拟「每天买 1~2 只」的实盘约束，
# 留一点余量便于观察排序质量。
DEFAULT_TOP_N = 10
# 权重绝对值低于此值的因子不参与——避免长尾噪声稀释主因子。
MIN_ABS_WEIGHT = 0.05


@dataclass(frozen=True)
class FactorWeight:
    """单个因子的合成配置。weight 已含方向：负值表示因子值越大越差。"""

    name: str
    weight: float

    @property
    def reversed_use(self) -> bool:
        return self.weight < 0


@dataclass
class ShadowScoreConfig:
    """影子池打分配置。默认权重来自 2026-08-22 首轮 IC 扫描的 T+10 结果。

    只收录三段方向全一致且通过可用门槛（|IC|>=0.02 且 |IC_IR|>=0.30）的因子。

    **因子名必须与 scripts/scan_factor_ic.build_factors 的键逐字一致**——影子池的分位
    面板由那个函数产出。2026-08-24 生产失败就源于此：默认权重曾写 dry_vol_min10_q250
    与 vol_ratio_5_20，而 main 上的键是 dry_vol_q250 / vol_ratio，脚本直接
    SystemExit「未知因子」。那两个名字来自一个未合并的本地版本，我按它写了权重却没
    在 main 上验证。tests 现有用例断言两侧键集一致，防止再次漂移。

    用 main 的真实因子定义重测（498 个交易日 / 3 段各约 75 日）后可用的是：

        ret60         IC -0.0350  IR -0.35  三段同号
        rps_slow      IC -0.0350  IR -0.35  三段同号
        dry_vol_q250  IC -0.0320  IR -0.32  三段同号

    rps_slow 与 ret60 的 IC 完全相同——前者就是后者的横截面分位，**只取其一**，
    否则同一信息被计权两次。rps_fast（IR -0.25）、vol_ratio（IR -0.24 且仅 2/3 段
    同向）未过门槛；turnover_amt / bias_200 / price_from_low250 等为正日占比落在
    45~55% 噪声带内，按 AGENTS.md 判为无方向性。
    """

    weights: tuple[FactorWeight, ...] = (
        # IC -0.0350 / IR -0.35，三段方向全一致
        FactorWeight("ret60", -0.35),
        # IC -0.0320 / IR -0.32，三段方向全一致
        FactorWeight("dry_vol_q250", -0.32),
    )
    top_n: int = DEFAULT_TOP_N
    min_avg_amount_wan: float = 8000.0
    horizon: int = 10

    def normalized(self) -> dict[str, float]:
        """归一化到绝对值和为 1，并剔除权重过小的因子。"""
        keep = [w for w in self.weights if abs(w.weight) >= MIN_ABS_WEIGHT]
        total = sum(abs(w.weight) for w in keep)
        if total <= 0:
            return {}
        return {w.name: w.weight / total for w in keep}

    def describe(self) -> str:
        parts = [f"{name}{weight:+.3f}" for name, weight in self.normalized().items()]
        return f"top{self.top_n} / T+{self.horizon} / " + " ".join(parts)


@dataclass
class ShadowPick:
    code: str
    score: float
    rank: int
    factor_ranks: dict[str, float] = field(default_factory=dict)

    def as_features(self) -> dict[str, object]:
        """写入 features_json 的内容，便于事后核对分数构成。"""
        return {
            "ic_shadow_score": round(self.score, 4),
            "ic_shadow_rank": self.rank,
            "factor_percentiles": {k: round(v, 2) for k, v in self.factor_ranks.items()},
        }


def combine_scores(
    factor_percentiles: dict[str, dict[str, float]],
    config: ShadowScoreConfig,
) -> list[ShadowPick]:
    """按权重合成分数并取 top-N。

    factor_percentiles: {因子名: {代码: 横截面分位 0~100}}。分位由调用方计算，
    这样核心逻辑可脱离 pandas 单测。
    """
    weights = config.normalized()
    if not weights:
        return []
    usable = {name: factor_percentiles[name] for name in weights if name in factor_percentiles}
    if not usable:
        return []
    # 只对所有可用因子都有值的标的打分——缺值补 50 会把无信息标的抬进 top-N。
    codes: set[str] | None = None
    for panel in usable.values():
        present = {code for code, value in panel.items() if value is not None and value == value}
        codes = present if codes is None else (codes & present)
    if not codes:
        return []

    scored: list[tuple[str, float, dict[str, float]]] = []
    for code in codes:
        ranks = {name: float(usable[name][code]) for name in usable}
        score = sum(weights[name] * ranks[name] for name in usable)
        scored.append((code, score, ranks))
    scored.sort(key=lambda item: -item[1])
    return [
        ShadowPick(code=code, score=score, rank=index + 1, factor_ranks=ranks)
        for index, (code, score, ranks) in enumerate(scored[: max(config.top_n, 0)])
    ]


def percentiles_from_df_map(
    df_map: dict,
    config: ShadowScoreConfig,
    min_avg_amount_wan: float | None = None,
) -> dict[str, dict[str, float]]:
    """直接用漏斗已抓的 df_map 算因子分位，避免为影子池重复抓一次全市场快照。

    原实现是独立 workflow 自己抓 560 天快照，实测每天 45 分钟——而漏斗本就抓了
    320 个交易日（FunnelConfig.trading_days），足够覆盖影子池最长的 250 日滚动分位
    （dry_vol_q250 需 250+20）。故改为复用。

    因子口径必须与 scripts/scan_factor_ic.build_factors 保持一致，否则影子池打分
    与 IC 结论脱节；tests 有用例断言两侧因子名一致。
    """
    import pandas as pd

    if not df_map:
        return {}
    frames = []
    for code, df in df_map.items():
        if df is None or getattr(df, "empty", True):
            continue
        frames.append((str(code), df))
    if not frames:
        return {}

    def _panel(column: str) -> pd.DataFrame:
        cols = {}
        for code, df in frames:
            if column in df.columns:
                cols[code] = pd.to_numeric(df[column], errors="coerce").reset_index(drop=True)
        return pd.DataFrame(cols) if cols else pd.DataFrame()

    close = _panel("close")
    vol = _panel("volume") if any("volume" in df.columns for _, df in frames) else _panel("vol")
    amount = _panel("amount")
    if close.empty:
        return {}

    threshold = config.min_avg_amount_wan if min_avg_amount_wan is None else min_avg_amount_wan
    eligible = close.columns
    if not amount.empty:
        # 与 scan_factor_ic 同口径：20 日均额（万元），只用截面日之前的数据。
        amt20 = amount.rolling(20).mean().iloc[-1] / 10000.0
        eligible = amt20[amt20 >= threshold].index

    panels: dict[str, dict[str, float]] = {}
    wanted = set(config.normalized())
    if "ret60" in wanted:
        panels["ret60"] = _last_percentile(close.pct_change(60, fill_method=None) * 100, eligible)
    if "ret20" in wanted:
        panels["ret20"] = _last_percentile(close.pct_change(20, fill_method=None) * 100, eligible)
    if "dry_vol_q250" in wanted and not vol.empty:
        v20 = vol.rolling(20).mean()
        panels["dry_vol_q250"] = _last_percentile(v20.rolling(250).rank(pct=True) * 100, eligible)
    if "vol_ratio" in wanted and not vol.empty:
        v20 = vol.rolling(20).mean()
        panels["vol_ratio"] = _last_percentile(vol / v20.replace(0, float("nan")), eligible)
    return panels


def _last_percentile(panel, eligible) -> dict[str, float]:
    """取最后一个截面日的横截面分位（0~100）。"""
    row = panel.iloc[-1].reindex(eligible).dropna()
    if row.empty:
        return {}
    return (row.rank(pct=True) * 100).to_dict()


def to_rows(picks: list[ShadowPick], trade_date: str, config: ShadowScoreConfig) -> list[dict]:
    """转成 signal_observations 行。signal_type 用 ic_shadow 便于与真实买点区分。

    放在 core 而非 scripts：workflows/wyckoff_funnel 要调它，而
    tests/test_architecture_boundaries 禁止 runtime 层依赖脚本入口。
    """
    import json

    return [
        {
            "market": "cn",
            "trade_date": trade_date,
            "code": pick.code.split(".")[0],
            "signal_type": SHADOW_SOURCE,
            "source": SHADOW_SOURCE,
            "channel": SHADOW_CHANNEL,
            "candidate_rank": pick.rank,
            "priority_score": round(pick.score, 4),
            # 影子池不进推荐、不下单——这两个标记确保下游不会误取。
            "ai_recommended": False,
            "selected_for_ai": False,
            "candidate_status": "shadow_observe",
            # track 是 NOT NULL，仅接受 Trend / Accum。影子池按 IC 反向打分选出的是
            # 低位缩量股（ret60 与 dry_vol_q250 分位均在个位数），语义上属吸筹，故填 Accum。
            # 2026-08-24 首次实盘落库因漏填此列被 Postgres 拒绝（not-null violation），
            # 当时容错生效、漏斗主流程未受影响。
            "track": "Accum",
            "stage": "",
            "strategy_version": config.describe(),
            "features_json": json.dumps(pick.as_features(), ensure_ascii=False),
        }
        for pick in picks
    ]
