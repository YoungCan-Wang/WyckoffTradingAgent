"""排序权重体检：``watch_score`` 里的 ``trigger_q``（生产 0.30，单项最大）。

结论与 dry_q 相反：**这一项是负贡献，但「该降多少」这个问题当前证据答不了**。
方向稳、幅度不稳，见下文「窗口长度改变了结论强度」。
详见 :func:`decision` 与 docs/evidence/trigger_weight_h*.md。

三个结构性事实，先于任何统计
----------------------------

**一、``trigger_score`` 的六个来源不同量纲，取 max 等于按类型排序。**
``core/candidate_ranker.py:_trigger_score_map`` 对六种触发取最大值，但
``core/wyckoff_engine.py`` 里每种的分数量纲完全不同（单类型命中的中位数）::

    trend_pullback 0.446   lps 0.580   compression 0.617
    evr            2.145   spring 2.146   sos 4.285

区块几乎不重叠。所以 ``trigger_q``（对 ``trigger_score`` 取分位）**首先按触发
类型排序，只在同类型内部才反映强弱**。把它当「Wyckoff 触发强度」用是误读。

**二、未触发的票 ``trigger_score`` 是 0.0，不是缺失。**
``candidate_score_value(None) -> 0.0``（core/candidate_policy.py），且
``_add_rank_quantiles`` 在 ``nunique <= 1`` 时退化成 ``1.0 if x > 0 else 0.0``。
触发率只有 3~6%，所以这个 0.30 权重项在绝大多数日子里近似一个二元指示器。

**三、九成以上命中只来自单一触发。** ``n_hits`` 分布 1:38060 / 2:1179 / 3:20，
即「多信号共振」在样本里几乎不存在，不能作为加分依据。

统计结论
--------

事实一二决定了要测三件事，而不是「排序是否校准」：

- Q1 二元：有触发 vs 同日流动性域，前向超额
- Q2 幅度：触发内部，``trigger_score`` 的排序比二元旗标多带多少
- Q3 类型：六种触发各自的超额

三项在两个 horizon 上一致指向负——有触发本身就是负超额，幅度排序不额外带信息，
六种触发无一为正。臂对照里 ``keep``（含 trigger_q）显著为负而 ``drop``（去掉它）
回到零附近。

窗口长度改变了结论强度
----------------------

首轮只有 192 个交易日（20251117..20260813），因为触发面板要 210 个 bar 预热，
把 2025 年整段吃掉了，实际只覆盖**一个行情段**。补齐到 423 日（20241118 起）后：

===================  ==============  ==============
量                   192 日           423 日
===================  ==============  ==============
Q1 触发超额 H=10     -0.774 (t=-4.67)  -0.483 (t=-4.94)
消融 top10 H=10      -1.461 (t=-3.14)  -0.735 (t=-2.29)
消融 top10 H=5       -1.019 (t=-2.84)  -0.424 (t=-1.68)
走前 top10 H=10      +1.275 (t=+2.14)  +0.599 (t=+1.96)
负季度               3/4               5/8
===================  ==============  ==============

**方向没变、显著性反而更强（Q1 的 t 从 -4.67 到 -4.94，因为样本翻倍），但幅度腰斩，
且走前挑权重掉到 t<2 —— 三道闸不再全过，所以 :func:`decision` 现在给「维持 0.30，
先观察一个季度」而不是首轮的「下调到 0.10」。**

这个落差本身是结论：首轮 -1.461 里有一半是单一行情段的运气。翻正的季度全在
样本两端（20244 +2.448、20263 +2.996），中间六季有五季为负，所以不是趋势反转，
是这一项在不同行情段的**幅度极不稳定**。要动生产权重得等走前那格自己过线，
别拿消融那格的显著性去替它——消融测的是「这一项好不好」，走前测的是
「换一档能不能真的更好」，后者才是上线依据。

口径约定
--------
- 前向收益：T+1 开盘买、T+1+H 收盘卖，扣单次往返成本 0.202%
- 域：20 日均额 >= 8000 万元（tushare ``amount`` 单位为千元，故阈值取 80000）
- 触发面板由 ``scripts/evaluate_trigger_weight.py`` 逐票逐日重放生产
  ``layer4_triggers`` 的六个检测器生成，只用 T 日及之前的 bar
- ``max_bias_200`` 取全局/科创板上限（``channel=""``）。生产在趋势通道上会放宽
  这个上限，本模块不复现 L2 通道，故触发率是**下界**
- 每日等权后再跨日平均；``excess`` 为相对同日流动性域内全体的超额
- t 值为手工计算（环境无 scipy）；Spearman 用「秩上的 Pearson」代替

为什么必须带随机负控制
----------------------
给动量分加**任何**一个 0.30 权重的搅动项都会把选择从动量极端端拉开。所以
「去掉 trigger_q 变好了」也不能直接归因，必须和同权重随机分位比：随机臂的
增量带宽 ``rand_band`` 就是无信息基准。trigger_q 的减益必须**比随机搅动更差**
才算它自己的问题，否则只是「加了个 0.30 噪声项」的通用代价。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

# 生产权重，用于在报告里标注「当前档位」。
PROD_TRIGGER_WEIGHT = 0.30
PROD_Q20_WEIGHT = 0.25
PROD_Q5_WEIGHT = 0.20
PROD_Q3_WEIGHT = 0.05
PROD_DRY_WEIGHT = 0.20

PROD_EXT_RET20_MIN = 45.0
PROD_EXT_RET20_SPAN = 55.0
PROD_EXT_RET20_MAX = 0.30
PROD_EXT_RET5_MIN = 18.0
PROD_EXT_RET5_SPAN = 22.0
PROD_EXT_RET5_MAX = 0.10

ROUND_TRIP_COST_PCT = 0.202
MIN_AMOUNT_RAW = 80000.0

# 网格含 0.0（等于删掉这一项）。上界给到 0.60 = 生产值两倍，
# 避免重演 dry_q 首轮「网格边界被当成单调向上」的假象。
WEIGHT_GRID = (0.0, 0.10, 0.20, 0.30, 0.45, 0.60)
TRIGGER_KINDS = ("sos", "spring", "lps", "evr", "compression", "trend_pullback")
# L3 代理：域内按动量基分取前 K 名。生产在 L3 存活集上算 trigger_q，
# 不是在流动性域上，所以必须验证负号在预筛过的强势池里是否依然成立。
L3_PROXY_SIZES = (300, 600)
TOP_N_GRID = (10, 20)
RANDOM_SEEDS = (1, 2, 3, 4, 5)

MIN_DAYS = 20
MIN_TRIGGERED = 5
MIN_HALF = 5
MIN_POOL_DAYS = 20
WALK_FORWARD_WARMUP = 60


def tstat(values: list[float]) -> float | None:
    """手工 t 值（环境无 scipy）。方差为零时返回 None，不返回 inf。"""
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(clean)
    if n < 3:
        return None
    avg = sum(clean) / n
    var = sum((v - avg) ** 2 for v in clean) / (n - 1)
    if var <= 0:
        return None
    return avg / math.sqrt(var / n)


def quarter_of(date: int) -> int:
    """20260815 -> 20263（年 * 10 + 季）。"""
    year, month = divmod(int(date) // 100, 100)
    return year * 10 + (month - 1) // 3 + 1


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def extension_penalty(ret20: float, ret5: float) -> float:
    """与 core/candidate_ranker.py:_extension_penalty_series 同式。

    基准臂必须带上它，否则基准臂比生产更天真，会把 trigger_q 的减益算大。
    """
    p20 = min(max((float(ret20) - PROD_EXT_RET20_MIN) / PROD_EXT_RET20_SPAN, 0.0), 1.0) * PROD_EXT_RET20_MAX
    p5 = min(max((float(ret5) - PROD_EXT_RET5_MIN) / PROD_EXT_RET5_SPAN, 0.0), 1.0) * PROD_EXT_RET5_MAX
    return p20 + p5


@dataclass
class BinaryStat:
    """Q1：有触发 vs 同日流动性域。回答「触发这件事本身值不值钱」。"""

    days: int
    avg_size: float
    inside: float | None
    inside_t: float | None
    excess: float | None
    excess_t: float | None

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.excess is None or self.excess_t is None:
            return "样本不足"
        if abs(self.excess_t) < 2.0:
            return "不显著：触发与否无差别"
        return "显著为正：触发本身有超额" if self.excess > 0 else "显著为负：触发本身是负超额"

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "avg_size": _round(self.avg_size, 1),
            "inside": _round(self.inside),
            "inside_t": _round(self.inside_t, 2),
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "verdict": self.verdict,
        }


@dataclass
class MagnitudeStat:
    """Q2：触发内部，``trigger_score`` 高低半区之差 + 秩相关。

    这一项决定「0.30 的连续分位项」是否比「0.30 的二元旗标」多带信息。
    ``spread`` 接近零且 ``ic`` 非正，说明分位排序在触发内部无判别力——
    考虑到量纲问题（六种触发不同尺度），这正是预期。
    """

    days: int
    spread: float | None
    spread_t: float | None
    ic: float | None
    ic_t: float | None

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.spread is None or self.spread_t is None:
            return "样本不足"
        if abs(self.spread_t) < 2.0:
            return "不显著：幅度不带信息"
        return "显著为正：幅度有判别力" if self.spread > 0 else "显著为负：幅度方向反了"

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "spread": _round(self.spread),
            "spread_t": _round(self.spread_t, 2),
            "ic": _round(self.ic),
            "ic_t": _round(self.ic_t, 2),
            "verdict": self.verdict,
        }


@dataclass
class KindStat:
    """Q3：单一触发类型的域内超额。

    存在的意义是排除「按类型重新配权就能救」这条路：若某几种为正、某几种为负,
    则该改 ``_trigger_score_map`` 的聚合方式而不是砍权重。
    """

    kind: str
    days: int
    avg_size: float
    excess: float | None
    excess_t: float | None

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.excess is None or self.excess_t is None:
            return "样本不足"
        if abs(self.excess_t) < 2.0:
            return "不显著"
        return "显著为正" if self.excess > 0 else "显著为负"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "days": self.days,
            "avg_size": _round(self.avg_size, 1),
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "verdict": self.verdict,
        }


@dataclass
class AblationStat:
    """四臂逐日配对：keep（生产）/ drop（权重置零）/ binary（换成二元旗标）/ rand。

    与 dry_q 版的判定方向相反：这里 ``diff = keep - drop``，**为负**才是可行动
    结论（砍权重）。所以三道闸也要反着写：
    显著为负、幅度抵得过成本、且比随机搅动更差（``worse_than_random``）。
    """

    top_n: int
    days: int
    keep: float | None
    keep_t: float | None
    drop: float | None
    drop_t: float | None
    diff: float | None
    diff_t: float | None
    binary: float | None = None
    binary_t: float | None = None
    keep_minus_binary: float | None = None
    keep_minus_binary_t: float | None = None
    overlap: float | None = None
    rand_min: float | None = None
    rand_max: float | None = None
    excess_by_quarter: dict[int, float] = field(default_factory=dict)

    @property
    def worse_than_random(self) -> bool | None:
        """减益是否超出同权重随机搅动的带宽。

        落在带内只说明「加了个 0.30 噪声项」，不能归因给 trigger_q 本身；
        但注意——即便落在带内，砍掉它仍然是对的，只是理由变成「这一项不比
        随机数强」而非「这一项有害」。
        """
        if self.diff is None or self.rand_min is None or self.rand_max is None:
            return None
        return self.diff < self.rand_min

    @property
    def negative_quarters(self) -> str:
        if not self.excess_by_quarter:
            return "—"
        neg = sum(1 for v in self.excess_by_quarter.values() if v < 0)
        return f"{neg}/{len(self.excess_by_quarter)}"

    @property
    def rank_adds_over_binary(self) -> bool | None:
        """连续分位是否比二元旗标多带信息。为假 -> 0.30 的分位项白给。"""
        if self.keep_minus_binary is None or self.keep_minus_binary_t is None:
            return None
        return self.keep_minus_binary > 0 and self.keep_minus_binary_t >= 2.0

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t >= 2.0:
            return "显著为正：支持保留"
        if self.diff_t > -2.0:
            return "不显著：这一项不值 0.30 权重"
        if abs(self.diff) <= ROUND_TRIP_COST_PCT:
            return "显著为负但幅度不抵成本"
        if self.worse_than_random is False:
            return "显著为负但落在随机带内：不比随机数更差，仍不值 0.30"
        return "显著为负且超出随机带：支持下调"

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_n": self.top_n,
            "days": self.days,
            "keep": _round(self.keep),
            "keep_t": _round(self.keep_t, 2),
            "drop": _round(self.drop),
            "drop_t": _round(self.drop_t, 2),
            "diff": _round(self.diff),
            "diff_t": _round(self.diff_t, 2),
            "binary": _round(self.binary),
            "binary_t": _round(self.binary_t, 2),
            "keep_minus_binary": _round(self.keep_minus_binary),
            "keep_minus_binary_t": _round(self.keep_minus_binary_t, 2),
            "rank_adds_over_binary": self.rank_adds_over_binary,
            "overlap": _round(self.overlap, 3),
            "rand_min": _round(self.rand_min),
            "rand_max": _round(self.rand_max),
            "worse_than_random": self.worse_than_random,
            "negative_quarters": self.negative_quarters,
            "excess_by_quarter": {str(k): _round(v, 3) for k, v in sorted(self.excess_by_quarter.items())},
            "verdict": self.verdict,
        }


@dataclass
class WeightStat:
    """某个候选权重下的域内超额。"""

    weight: float
    days: int
    inside: float | None
    inside_t: float | None
    excess: float | None
    excess_t: float | None

    @property
    def is_production(self) -> bool:
        return abs(self.weight - PROD_TRIGGER_WEIGHT) < 1e-9

    def as_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight,
            "days": self.days,
            "inside": _round(self.inside),
            "inside_t": _round(self.inside_t, 2),
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "is_production": self.is_production,
        }


@dataclass
class PoolStat:
    """L3 代理：域内按动量基分取前 K 名后再算 keep-drop。

    生产 ``rank_l3_candidates`` 排的是 L3 存活集，触发池不等于流动性域。
    这一格验证负号在预筛过的强势池里是否依然成立。
    """

    pool_size: int
    top_n: int
    days: int
    trigger_rate: float | None
    diff: float | None
    diff_t: float | None

    @property
    def verdict(self) -> str:
        if self.days < MIN_POOL_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t <= -2.0:
            return "池内仍显著为负"
        if self.diff_t >= 2.0:
            return "池内转显著为正"
        return "池内不显著"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool_size": self.pool_size,
            "top_n": self.top_n,
            "days": self.days,
            "trigger_rate": _round(self.trigger_rate, 4),
            "diff": _round(self.diff),
            "diff_t": _round(self.diff_t, 2),
            "verdict": self.verdict,
        }


@dataclass
class WalkForwardStat:
    """走前动态选权重 vs 固定生产权重。"""

    top_n: int
    days: int
    chosen: float | None
    fixed: float | None
    diff: float | None
    diff_t: float | None
    pick_dist: dict[float, float] = field(default_factory=dict)

    @property
    def is_concentrated(self) -> bool | None:
        """选中分布是否集中在单一权重上。"""
        if not self.pick_dist:
            return None
        return max(self.pick_dist.values()) >= 0.80

    @property
    def picks_below_production(self) -> float | None:
        """选中低于生产权重的比例。

        这是本轮的关键判据，也是与 dry_q 那轮的分水岭：即使 ``is_concentrated``
        为假，只要几乎所有选中都落在生产值**以下**，「下调」这个方向就是稳的,
        只是「下调到具体哪一档」不稳。dry_q 那轮的散开跨越 0.40/0.80/1.20 三档
        （3 倍量级），方向本身都读不出来，才必须否掉。
        """
        if not self.pick_dist:
            return None
        return sum(v for k, v in self.pick_dist.items() if k < PROD_TRIGGER_WEIGHT)

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t < 2.0:
            return "走前不显著：不足以支持改动"
        below = self.picks_below_production
        if below is not None and below >= 0.80:
            if self.is_concentrated:
                return "走前显著且选中集中在生产值以下：支持下调"
            return "走前显著、选中均在生产值以下但档位散开：支持下调，具体档位待定"
        if not self.is_concentrated:
            return "走前显著但选中分布散开：疑似拟合噪声，不可上线"
        return "走前显著：值得进一步验证"

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_n": self.top_n,
            "days": self.days,
            "chosen": _round(self.chosen),
            "fixed": _round(self.fixed),
            "diff": _round(self.diff),
            "diff_t": _round(self.diff_t, 2),
            "pick_dist": {str(k): _round(v, 3) for k, v in sorted(self.pick_dist.items())},
            "is_concentrated": self.is_concentrated,
            "picks_below_production": _round(self.picks_below_production, 3),
            "verdict": self.verdict,
        }


@dataclass
class TriggerReport:
    binary: BinaryStat | None = None
    magnitude: MagnitudeStat | None = None
    kinds: list[KindStat] = field(default_factory=list)
    ablation: list[AblationStat] = field(default_factory=list)
    weights: dict[int, list[WeightStat]] = field(default_factory=dict)
    pools: list[PoolStat] = field(default_factory=list)
    walk_forward: list[WalkForwardStat] = field(default_factory=list)
    kind_medians: dict[str, float] = field(default_factory=dict)
    hits_dist: dict[int, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "q1_binary": self.binary.as_dict() if self.binary else {},
            "q2_magnitude": self.magnitude.as_dict() if self.magnitude else {},
            "q3_kinds": [k.as_dict() for k in self.kinds],
            "ablation": [a.as_dict() for a in self.ablation],
            "weight_grid": {str(k): [w.as_dict() for w in v] for k, v in sorted(self.weights.items())},
            "l3_proxy_pools": [p.as_dict() for p in self.pools],
            "walk_forward": [w.as_dict() for w in self.walk_forward],
            "scale_check": {
                "kind_score_medians": {k: _round(v, 3) for k, v in sorted(self.kind_medians.items())},
                "n_hits_dist": {str(k): v for k, v in sorted(self.hits_dist.items())},
                "note": (
                    "六种触发分数量纲不同，取 max 等于先按类型排序；九成以上命中只来自单一触发，"
                    "「多信号共振」在样本里几乎不存在"
                ),
            },
            "production": {
                "trigger_weight": PROD_TRIGGER_WEIGHT,
                "q20_weight": PROD_Q20_WEIGHT,
                "q5_weight": PROD_Q5_WEIGHT,
                "q3_weight": PROD_Q3_WEIGHT,
                "dry_weight": PROD_DRY_WEIGHT,
            },
            "cost_threshold_pct": ROUND_TRIP_COST_PCT,
            "reading": (
                "excess 为相对同日流动性域内全体的超额。消融 diff = keep - drop，"
                "**为负**才是可行动结论（砍权重），须同时超过成本 0.202% 并低于随机带下沿。"
                "keep_minus_binary 近零说明 0.30 的连续分位项不比二元旗标多带信息。"
                '触发面板用 channel="" 重放，max_bias_200 取全局上限，故触发率是下界。'
            ),
        }


def summarize_binary(daily: list[dict[str, float]]) -> BinaryStat:
    """Q1 汇总。每日等权，避免触发只数多的日子主导均值。"""
    usable = [r for r in daily if r.get("inside") is not None and r.get("domain") is not None]
    if len(usable) < MIN_DAYS:
        return BinaryStat(len(usable), 0.0, None, None, None, None)
    ins = [float(r["inside"]) for r in usable]
    exc = [float(r["inside"]) - float(r["domain"]) for r in usable]
    return BinaryStat(
        days=len(usable),
        avg_size=mean(float(r.get("size") or 0) for r in usable),
        inside=mean(ins),
        inside_t=tstat(ins),
        excess=mean(exc),
        excess_t=tstat(exc),
    )


def summarize_magnitude(daily: list[dict[str, float]]) -> MagnitudeStat:
    """Q2 汇总：高半区减低半区，外加逐日 Spearman IC 的均值与 t。"""
    usable = [r for r in daily if r.get("spread") is not None]
    if len(usable) < MIN_DAYS:
        return MagnitudeStat(len(usable), None, None, None, None)
    spreads = [float(r["spread"]) for r in usable]
    ics = [float(r["ic"]) for r in usable if r.get("ic") is not None]
    return MagnitudeStat(
        days=len(usable),
        spread=mean(spreads),
        spread_t=tstat(spreads),
        ic=mean(ics) if ics else None,
        ic_t=tstat(ics) if ics else None,
    )


def summarize_kind(kind: str, daily: list[dict[str, float]]) -> KindStat:
    usable = [r for r in daily if r.get("inside") is not None and r.get("domain") is not None]
    if len(usable) < MIN_DAYS:
        return KindStat(kind, len(usable), 0.0, None, None)
    exc = [float(r["inside"]) - float(r["domain"]) for r in usable]
    return KindStat(
        kind=kind,
        days=len(usable),
        avg_size=mean(float(r.get("size") or 0) for r in usable),
        excess=mean(exc),
        excess_t=tstat(exc),
    )


def summarize_ablation(
    top_n: int,
    rows: list[dict[str, float]],
    rand_diffs: list[float] | None = None,
) -> AblationStat:
    """逐日配对：同日同域同动量基分，只差 trigger_q 一项。配对消掉市场共同成分。"""
    usable = [r for r in rows if r.get("keep") is not None and r.get("drop") is not None]
    if len(usable) < MIN_DAYS:
        return AblationStat(top_n, len(usable), None, None, None, None, None, None)
    keep = [float(r["keep"]) for r in usable]
    drop = [float(r["drop"]) for r in usable]
    diff = [k - d for k, d in zip(keep, drop, strict=True)]
    by_q: dict[int, list[float]] = {}
    for row, value in zip(usable, diff, strict=True):
        by_q.setdefault(quarter_of(int(row["date"])), []).append(value)
    binary_rows = [r for r in usable if r.get("binary") is not None]
    binary = [float(r["binary"]) for r in binary_rows]
    kmb = [float(r["keep"]) - float(r["binary"]) for r in binary_rows]
    band = [float(v) for v in (rand_diffs or []) if v is not None and math.isfinite(float(v))]
    overlaps = [float(r["overlap"]) for r in usable if r.get("overlap") is not None]
    return AblationStat(
        top_n=top_n,
        days=len(usable),
        keep=mean(keep),
        keep_t=tstat(keep),
        drop=mean(drop),
        drop_t=tstat(drop),
        diff=mean(diff),
        diff_t=tstat(diff),
        binary=mean(binary) if binary else None,
        binary_t=tstat(binary) if binary else None,
        keep_minus_binary=mean(kmb) if kmb else None,
        keep_minus_binary_t=tstat(kmb) if kmb else None,
        overlap=mean(overlaps) if overlaps else None,
        rand_min=min(band) if band else None,
        rand_max=max(band) if band else None,
        excess_by_quarter={q: mean(v) for q, v in sorted(by_q.items())},
    )


def summarize_weight(weight: float, rows: list[dict[str, float]]) -> WeightStat:
    usable = [r for r in rows if r.get("inside") is not None and r.get("domain") is not None]
    if len(usable) < MIN_DAYS:
        return WeightStat(weight, len(usable), None, None, None, None)
    ins = [float(r["inside"]) for r in usable]
    exc = [float(r["inside"]) - float(r["domain"]) for r in usable]
    return WeightStat(weight, len(usable), mean(ins), tstat(ins), mean(exc), tstat(exc))


def summarize_pool(pool_size: int, top_n: int, rows: list[dict[str, float]]) -> PoolStat:
    usable = [r for r in rows if r.get("keep") is not None and r.get("drop") is not None]
    if len(usable) < MIN_POOL_DAYS:
        return PoolStat(pool_size, top_n, len(usable), None, None, None)
    diff = [float(r["keep"]) - float(r["drop"]) for r in usable]
    rates = [float(r["rate"]) for r in usable if r.get("rate") is not None]
    return PoolStat(
        pool_size=pool_size,
        top_n=top_n,
        days=len(usable),
        trigger_rate=mean(rates) if rates else None,
        diff=mean(diff),
        diff_t=tstat(diff),
    )


def walk_forward_weight(
    top_n: int,
    dates: list[int],
    by_weight: dict[float, list[float]],
    *,
    horizon: int,
    warmup: int = WALK_FORWARD_WARMUP,
) -> WalkForwardStat:
    """走前挑权重：T 日只用**已结算**的历史（截到 T-H-1），再跟固定生产权重比。

    截到 T-H-1 而不是 T-1 是必须的：T-H..T-1 的前向收益在 T 日还没结算,
    用了就是未来信息。
    """
    lag = int(horizon) + 1
    if not by_weight or PROD_TRIGGER_WEIGHT not in by_weight:
        return WalkForwardStat(top_n, 0, None, None, None, None, {})
    chosen: list[float] = []
    fixed: list[float] = []
    picks: list[float] = []
    for i in range(warmup, len(dates)):
        end = i - lag
        if end <= MIN_DAYS:
            continue
        best_w, best_v = PROD_TRIGGER_WEIGHT, -math.inf
        for weight, series in by_weight.items():
            hist = series[:end]
            if not hist:
                continue
            value = sum(hist) / len(hist)
            if value > best_v:
                best_w, best_v = weight, value
        chosen.append(float(by_weight[best_w][i]))
        fixed.append(float(by_weight[PROD_TRIGGER_WEIGHT][i]))
        picks.append(best_w)
    if len(chosen) < MIN_DAYS:
        return WalkForwardStat(top_n, len(chosen), None, None, None, None, {})
    diffs = [c - f for c, f in zip(chosen, fixed, strict=True)]
    dist: dict[float, int] = {}
    for pick in picks:
        dist[pick] = dist.get(pick, 0) + 1
    return WalkForwardStat(
        top_n=top_n,
        days=len(chosen),
        chosen=mean(chosen),
        fixed=mean(fixed),
        diff=mean(diffs),
        diff_t=tstat(diffs),
        pick_dist={k: v / len(picks) for k, v in dist.items()},
    )


def _signed(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.{digits}f}"


def _plain(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _scale_section(medians: dict[str, float], hits: dict[int, int]) -> list[str]:
    lines = ["### 量纲体检（先于统计）", ""]
    if medians:
        lines += [
            _row(["触发类型", "单类型命中分数中位数"]),
            _row(["---"] * 2),
        ]
        for kind, value in sorted(medians.items(), key=lambda kv: kv[1]):
            lines.append(_row([kind, _plain(value, 3)]))
        lines += ["", "六种分数量纲不重叠，取 max 等于**先按类型排序**，只在同类型内部才反映强弱。", ""]
    if hits:
        total = sum(hits.values())
        share = ", ".join(f"{k} 种={v}（{v / total * 100:.1f}%）" for k, v in sorted(hits.items()))
        lines += [f"同日命中触发种数分布：{share}。「多信号共振」在样本里几乎不存在，不能作为加分依据。", ""]
    return lines


def _question_section(binary: BinaryStat | None, magnitude: MagnitudeStat | None) -> list[str]:
    lines = ["### Q1 二元：有触发 vs 同日流动性域", ""]
    if binary is None or binary.days < MIN_DAYS:
        lines += ["样本不足。", ""]
    else:
        lines += [
            _row(["天数", "均只数", "触发组", "t", "超额", "t", "判定"]),
            _row(["---"] * 7),
            _row(
                [
                    str(binary.days),
                    _plain(binary.avg_size, 1),
                    _signed(binary.inside),
                    _plain(binary.inside_t),
                    _signed(binary.excess),
                    _plain(binary.excess_t),
                    binary.verdict,
                ]
            ),
            "",
        ]
    lines += ["### Q2 幅度：触发内部高低半区之差", ""]
    if magnitude is None or magnitude.days < MIN_DAYS:
        lines += ["样本不足。", ""]
    else:
        lines += [
            _row(["天数", "高半区-低半区", "t", "Spearman IC", "t", "判定"]),
            _row(["---"] * 6),
            _row(
                [
                    str(magnitude.days),
                    _signed(magnitude.spread),
                    _plain(magnitude.spread_t),
                    _signed(magnitude.ic, 4),
                    _plain(magnitude.ic_t),
                    magnitude.verdict,
                ]
            ),
            "",
            "IC 用「秩上的 Pearson」代替 Spearman（环境无 scipy）。",
            "",
        ]
    return lines


def _kind_table(kinds: list[KindStat]) -> list[str]:
    lines = [
        "### Q3 类型：六种触发各自的域内超额",
        "",
        _row(["触发类型", "天数", "均只数", "超额", "t", "判定"]),
        _row(["---"] * 6),
    ]
    for stat in sorted(kinds, key=lambda k: (k.excess is None, k.excess or 0.0)):
        lines.append(
            _row(
                [
                    stat.kind,
                    str(stat.days),
                    _plain(stat.avg_size, 1),
                    _signed(stat.excess),
                    _plain(stat.excess_t),
                    stat.verdict,
                ]
            )
        )
    lines += ["", "只统计**单一类型**命中的样本，避免多类型命中被重复计入。", ""]
    return lines


def _ablation_table(stats: list[AblationStat]) -> list[str]:
    lines = [
        "### 四臂消融（逐日配对，只差 trigger_q 一项）",
        "",
        _row(["topN", "天数", "keep", "t", "drop", "t", "binary", "t", "keep−drop", "t", "keep−binary", "t"]),
        _row(["---"] * 12),
    ]
    for stat in stats:
        lines.append(
            _row(
                [
                    f"top{stat.top_n}",
                    str(stat.days),
                    _signed(stat.keep),
                    _plain(stat.keep_t),
                    _signed(stat.drop),
                    _plain(stat.drop_t),
                    _signed(stat.binary),
                    _plain(stat.binary_t),
                    _signed(stat.diff),
                    _plain(stat.diff_t),
                    _signed(stat.keep_minus_binary),
                    _plain(stat.keep_minus_binary_t),
                ]
            )
        )
    lines += [
        "",
        _row(["topN", "重叠率", "随机带", "低于随机带", "分位胜过二元", "负季度", "判定"]),
        _row(["---"] * 7),
    ]
    for stat in stats:
        band = "—"
        if stat.rand_min is not None and stat.rand_max is not None:
            band = f"{_signed(stat.rand_min)}~{_signed(stat.rand_max)}"
        lines.append(
            _row(
                [
                    f"top{stat.top_n}",
                    _pct(stat.overlap),
                    band,
                    {True: "是", False: "否", None: "—"}[stat.worse_than_random],
                    {True: "是", False: "否", None: "—"}[stat.rank_adds_over_binary],
                    stat.negative_quarters,
                    stat.verdict,
                ]
            )
        )
    quarters = sorted({q for s in stats for q in s.excess_by_quarter})
    if quarters:
        lines += ["", _row(["topN"] + [str(q) for q in quarters]), _row(["---"] * (len(quarters) + 1))]
        for stat in stats:
            cells = [_signed(stat.excess_by_quarter.get(q), 2) for q in quarters]
            lines.append(_row([f"top{stat.top_n}"] + cells))
    lines.append("")
    return lines


def _weight_table(weights: dict[int, list[WeightStat]]) -> list[str]:
    lines = ["### 权重网格（域内超额）", ""]
    for top_n, stats in sorted(weights.items()):
        lines += [
            f"top{top_n}：",
            "",
            _row(["权重", "天数", "组内", "t", "超额", "t"]),
            _row(["---"] * 6),
        ]
        for stat in stats:
            mark = " ← 生产" if stat.is_production else ""
            lines.append(
                _row(
                    [
                        f"{stat.weight:.2f}{mark}",
                        str(stat.days),
                        _signed(stat.inside),
                        _plain(stat.inside_t),
                        _signed(stat.excess),
                        _plain(stat.excess_t),
                    ]
                )
            )
        lines.append("")
    lines += ["网格上界给到 0.60（生产值两倍），避免窄网格把边界值读成单调最优。", ""]
    return lines


def _pool_table(pools: list[PoolStat]) -> list[str]:
    lines = [
        "### L3 代理：域内按动量基分预筛后重测",
        "",
        _row(["池大小", "topN", "天数", "池内触发率", "keep−drop", "t", "判定"]),
        _row(["---"] * 7),
    ]
    for pool in pools:
        lines.append(
            _row(
                [
                    str(pool.pool_size),
                    f"top{pool.top_n}",
                    str(pool.days),
                    _pct(pool.trigger_rate),
                    _signed(pool.diff),
                    _plain(pool.diff_t),
                    pool.verdict,
                ]
            )
        )
    lines += [
        "",
        "生产 `rank_l3_candidates` 排的是 L3 存活集，不是流动性域，且会在池内重算分位——"
        "这一格照同样口径在池内重算 `trigger_q`。",
        "",
    ]
    return lines


def _walk_forward_table(stats: list[WalkForwardStat]) -> list[str]:
    lines = [
        "### 走前挑权重 vs 固定生产权重",
        "",
        _row(["topN", "天数", "走前", "固定", "增量", "t", "选中集中", "选中 < 生产值", "判定"]),
        _row(["---"] * 9),
    ]
    for stat in stats:
        lines.append(
            _row(
                [
                    f"top{stat.top_n}",
                    str(stat.days),
                    _signed(stat.chosen),
                    _signed(stat.fixed),
                    _signed(stat.diff),
                    _plain(stat.diff_t),
                    {True: "是", False: "否", None: "—"}[stat.is_concentrated],
                    _pct(stat.picks_below_production),
                    stat.verdict,
                ]
            )
        )
    for stat in stats:
        if stat.pick_dist:
            dist = ", ".join(f"{k:.2f}={_pct(v)}" for k, v in sorted(stat.pick_dist.items()))
            lines.append(f"- top{stat.top_n} 选中分布：{dist}")
    lines.append("")
    return lines


def decision(report: TriggerReport) -> list[str]:
    """把三问 + 消融 + 走前收成可执行结论。

    分级的理由：这一轮和 dry_q 那轮不同，结论指向**改生产参数**，所以判据要
    比「保留现状」更严。要求同时满足：消融显著为负、走前显著、且走前选中几乎
    全落在生产值以下。任一不满足就降级为「观察」，不给改动背书。
    """
    lines: list[str] = []
    binary = report.binary
    if binary is None or binary.days < MIN_DAYS:
        lines.append("① 触发本身：样本不足，本轮无结论。")
    elif binary.excess is not None and binary.excess_t is not None and binary.excess_t <= -2.0:
        lines.append(
            f"① 触发本身是**负超额**：{_signed(binary.excess)}（t={_plain(binary.excess_t)}），"
            "日均 " + _plain(binary.avg_size, 0) + " 只。所以问题不在排序校准，在这一层信号本身。"
        )
    elif binary.excess is not None and binary.excess_t is not None and binary.excess_t >= 2.0:
        lines.append(f"① 触发本身有超额：{_signed(binary.excess)}（t={_plain(binary.excess_t)}），可继续用。")
    else:
        lines.append("① 触发本身不显著：有触发与没触发无差别，这一项难以支撑 0.30 的最大权重。")

    mag = report.magnitude
    if mag is not None and mag.days >= MIN_DAYS and mag.spread_t is not None and abs(mag.spread_t) < 2.0:
        lines.append(
            f"② 幅度不带信息：高低半区差 {_signed(mag.spread)}（t={_plain(mag.spread_t)}）、"
            f"IC {_signed(mag.ic, 4)}。连续分位项相对二元旗标是白给的复杂度。"
        )
    elif mag is not None and mag.days >= MIN_DAYS:
        lines.append(f"② 幅度有方向：高低半区差 {_signed(mag.spread)}（t={_plain(mag.spread_t)}），值得单独看。")
    else:
        lines.append("② 幅度：样本不足。")

    ready_kinds = [k for k in report.kinds if k.days >= MIN_DAYS and k.excess is not None]
    positives = [k for k in ready_kinds if k.excess is not None and k.excess > 0]
    if not ready_kinds:
        lines.append("③ 类型：样本不足。")
    elif not positives:
        lines.append(
            f"③ {len(ready_kinds)}/{len(ready_kinds)} 种触发超额均为负，**没有一种为正**。"
            "所以这不是「按类型重新配权」能修的问题，改 `_trigger_score_map` 的聚合方式解决不了。"
        )
    else:
        names = "、".join(k.kind for k in positives)
        lines.append(
            f"③ 类型有分化：{names} 为正、其余为负。优先考虑改 `_trigger_score_map` 的聚合方式"
            "（按类型分别配权），而不是一刀砍总权重。"
        )

    lines.append(_weight_action(report.ablation, report.walk_forward))
    lines.append(
        f"⑤ 任何参数改动落地前，需确认幅度大于单次往返成本 {ROUND_TRIP_COST_PCT}%，"
        "且跨行情段方向稳定；本模块每周重跑，季度表出现翻正要复核。"
    )
    return lines


def _weight_action(ablation: list[AblationStat], walk_forward: list[WalkForwardStat]) -> str:
    """④ 是否动生产权重。三道闸全过才给「支持下调」。"""
    ready_ab = [s for s in ablation if s.days >= MIN_DAYS and s.diff_t is not None]
    ready_wf = [s for s in walk_forward if s.days >= MIN_DAYS and s.diff_t is not None]
    if not ready_ab or not ready_wf:
        return f"④ 权重维持 {PROD_TRIGGER_WEIGHT}：样本不足，先补数据。"
    neg = [s for s in ready_ab if s.diff_t is not None and s.diff_t <= -2.0]
    wf_pass = [
        s for s in ready_wf if s.diff_t is not None and s.diff_t >= 2.0 and (s.picks_below_production or 0.0) >= 0.80
    ]
    if not neg:
        return f"④ 权重维持 {PROD_TRIGGER_WEIGHT}：消融没有一格显著为负，不构成下调依据。"
    if not wf_pass:
        near = [s for s in ready_wf if s.diff_t is not None and 1.80 <= s.diff_t < 2.0]
        tail = ""
        if near:
            cells = "、".join(f"top{s.top_n} t={s.diff_t:+.2f}" for s in near)
            # t=1.96 是双侧 5% 的临界点，差 0.04 就四舍五入放行的话，这道闸等于没有。
            tail = f"（{cells} 贴着线但没过，不四舍五入；样本再长一点自己会说话）"
        return (
            f"④ 权重暂维持 {PROD_TRIGGER_WEIGHT}：消融有 {len(neg)}/{len(ready_ab)} 格显著为负，"
            f"但走前动态选权重未通过（t<2 或选中未集中于生产值以下）{tail}。"
            "方向存疑，先观察一个季度。"
        )
    cells = "、".join(f"top{s.top_n}" for s in wf_pass)
    return (
        f"④ **支持把 trigger_q 权重从 {PROD_TRIGGER_WEIGHT} 下调至 0.0~0.10**："
        f"消融 {len(neg)}/{len(ready_ab)} 格显著为负，走前在 {cells} 显著（t>2）且选中几乎全在生产值以下。"
        "注意 topN 之间不对称——只在集中持仓档成立，宽档不显著；"
        "落地方式建议先降到 0.10 观察，而不是直接置 0，保留「有触发」这点弱先验。"
    )


def render(report: TriggerReport, *, horizon: int, start: int, end: int) -> str:
    """渲染 markdown。落到 docs/evidence，不要落 artifacts/（已被 gitignore）。"""
    lines = [
        "# 排序权重体检：trigger_q",
        "",
        f"- 区间：{start} ~ {end}，前向 H={horizon} 日（T+1 开盘买、T+1+H 收盘卖）",
        # tushare amount 单位为千元，除 10 得万元
        f"- 域：20 日均额 >= {MIN_AMOUNT_RAW / 10:.0f} 万元；成本 {ROUND_TRIP_COST_PCT}%/次往返",
        f"- 生产权重：trigger_q={PROD_TRIGGER_WEIGHT}（单项最大），q20={PROD_Q20_WEIGHT}，"
        f"q5={PROD_Q5_WEIGHT}，q3={PROD_Q3_WEIGHT}，dry_q={PROD_DRY_WEIGHT}",
        "- 打分臂含 extension_penalty，与生产同式",
        "- 触发面板逐票逐日重放生产 `layer4_triggers` 的六个检测器，只用 T 日及之前的 bar",
        '- 重放用 `channel=""`，`max_bias_200` 取全局/科创板上限；生产在趋势通道上会放宽，故此处触发率是**下界**',
        "",
    ]
    lines += _scale_section(report.kind_medians, report.hits_dist)
    lines += _question_section(report.binary, report.magnitude)
    lines += _kind_table(report.kinds)
    lines += _ablation_table(report.ablation)
    lines += _weight_table(report.weights)
    lines += _pool_table(report.pools)
    lines += _walk_forward_table(report.walk_forward)
    lines += [
        "## 读法",
        "",
        "- `超额` 是相对**同日流动性域内全体**的超额，不是相对基准臂的增量。"
        "两个含噪臂相减等于把噪声算两遍，会让随机带宽到吞掉真信号。",
        "- 消融的 `keep−drop` 为「留 trigger_q 减去 trigger_q」的逐日配对值。"
        "与 dry_q 那轮方向相反：这里**为负**才是可行动结论。",
        "- `随机带` 是同权重随机分位臂的取值范围（逐日独立种子）。给动量分加**任何**一个 0.30 "
        "搅动项都会把选择从动量极端端拉开、本身就值几毛钱，所以减益必须低于带下沿才能归因给 trigger_q；"
        "落在带内则结论弱化为「不比随机数强」，但同样不支持 0.30 这个档位。",
        "- `keep−binary` 近零 = 连续分位项不比二元旗标多带信息，0.30 的排序精度是白给的。",
        "- `选中 < 生产值` 是本轮关键判据。即使档位散开，只要选中几乎全落在生产值以下，"
        "「下调」的方向就是稳的，只有「下调到哪一档」不稳。",
        "- L3 代理格验证负号在动量预筛后的强势池里是否依然成立，因为生产排的是 L3 存活集。",
        "",
        "## 接下来做什么",
        "",
    ]
    lines += decision(report)
    lines.append("")
    return "\n".join(lines)
