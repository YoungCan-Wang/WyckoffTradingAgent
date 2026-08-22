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
    2026-08-22 用生产 FunnelConfig 真实参数重测后，满足两条的恰好是这三个：
    rps_fast（20 日横截面分位）、ret60、dry_vol_min10_q250（10 日最低量 / 250 日分位）。

    vol_ratio_5_20 虽 IR -0.33 通过门槛，但只有 2/3 段同向，故不纳入——方向不稳的
    因子进模型等于引入拟合。turnover_amt / bias_200 等为正日占比落在 45~55%
    噪声带内，按 AGENTS.md 判定为无方向性。
    """

    weights: tuple[FactorWeight, ...] = (
        # IC -0.0711 / IR -0.38，各段 -0.084 -0.058 -0.072
        FactorWeight("rps_fast", -0.38),
        # IC -0.0692 / IR -0.37，各段 -0.086 -0.051 -0.071
        FactorWeight("ret60", -0.37),
        # IC -0.0504 / IR -0.35，各段 -0.070 -0.017 -0.063
        FactorWeight("dry_vol_min10_q250", -0.35),
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
