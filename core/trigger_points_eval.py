"""触发分值体检：``_trigger_score`` 那六个硬编码分值（12~50，路径 B）。

前两轮（dry_q / trigger_q）审的是 ``watch_score`` 里的权重，那是**路径 A**。
追踪最终排序时发现同一个触发信号进了两次，两条路径量级差一个数量级::

    路径 A  trigger_q(0.30) -> l3_score_map -> _layer3_rank_bonus
            = min(max(score,0),1.2) * 8.0        全摆幅 2.4 分
    路径 B  _trigger_score()  直接加在 score() 上   12 ~ 50 分

``core/ai_candidate_allocation.py:518`` 加路径 B，``:531`` 加路径 A，两者独立。
一次 lps 命中(30 分)等于 trigger_q 整个摆幅的 12.5 倍。所以前两轮量到的是小头,
真正决定「同日已触发的票谁排前面」的是这六个数。本模块审它们。

一个反常发现，先于任何建议
--------------------------

**生产这六个分值的排法,比随机打乱它们自己更差。** 200 次置换(把同一组数字在
六个类别间重排)给出无信息基准带,生产落在带的**下沿**::

    H=10  生产 -0.8513  置换带 -0.8513 ~ -0.2366  中位 -0.4874  -> 第 0.5 百分位
    H=5   生产 -0.5906  置换带 -0.5906 ~ -0.1788  中位 -0.3174  -> 第 0.5 百分位

生产恰好等于带的最小值,是因为 12.0 在表里出现两次(spring/evr),有若干置换与
生产同构。换句话说:**不存在比生产更差的排法**。这不是「校准不准」,是**反校准**
——分值高低与实测超额的秩相关为负(H=10 -0.290 / H=5 -0.406)。

拍平(六类同分 25)比生产好 +0.339(t=+3.58, H=10) / +0.267(t=+3.72, H=5),
两个 horizon 同向且都过 3 个标准差。

并列打破方式会改变结论强度，别用 nlargest
------------------------------------------

六个分值只组合出 22 个唯一分数,每日约 140 行触发,于是 top10 的边界分数桶
中位有 **17 只票**(最大 523)。``DataFrame.nlargest`` 按 index 顺序打破并列,
而 index 跟着 ``ts_code`` 排、``ts_code`` 又与交易所/板块相关 —— 等于偷偷按
代码字典序选票,把对照稀释掉::

    H=10  nlargest    拍平-生产 +0.131 (t=+0.86)   <- 读不出来
          并列分权    拍平-生产 +0.339 (t=+3.58)

本模块用 :func:`topn_mean` 对边界桶**按比例分权**(总权重恰为 N),这就是随机
打破并列的期望值,且确定可复。分值型排序键都有这个问题,分位型(trigger_q)没有。

六类的实测超额:分值给反了
--------------------------

单类型与多类型共振分开看(共振样本仅 833 行,占 sos 的 15%)::

    类别             生产分   H=10 超额         H=5 超额
    sos(单独)         15.0   -0.798 (t=-2.49)  -0.450 (t=-1.79)
    sos(共振)         50.0   -1.007 (t=-1.79)  -0.768 (t=-1.62)
    spring            12.0   -0.305 (t=-1.23)  -0.081 (t=-0.40)
    lps               30.0   -0.358 (t=-1.32)  -0.330 (t=-1.64)
    evr               12.0   -0.411 (t=-2.57)  -0.140 (t=-1.16)
    compression       22.0   -0.347 (t=-1.47)  -0.379 (t=-2.26)
    trend_pullback    34.0   -0.464 (t=-2.54)  -0.188 (t=-1.20)

**最高分那一档(sos 共振 50)超额最差**,而最低分档(spring 12)最接近零。共振加成
从 15 跳到 50 是全表最大的一次加分,方向却是反的 —— 与「多信号共振更可靠」的
直觉相反,和 trigger_q 那轮查到的 ``n_hits`` 分布(共振几乎不存在)是同一件事。

三道闸不变,不因为反常就放行
----------------------------

反校准是个强结论,但**改生产分值仍要过和前两轮同样的三道闸**:

1. 消融显著:替代表(拍平/按超额重排)优于生产,且超出成本与随机带
2. 走前 t>=2:逐日只用已结算历史挑表,选出来的比固定生产表更好
3. 选中集中:走前挑中的表不能在多个候选间乱跳

第 1 闸已过。第 2、3 闸由 :func:`walk_forward_table` 计算,结论见
:func:`decision` 与 docs/evidence/trigger_points_h*.md。

只改一条路径
------------
路径 A(trigger_q 0.30)那轮的结论是**维持**,本轮若改路径 B,两条路径不可同时
下调 —— 同一个信号被罚两次,净效果会超过任一轮量到的幅度。

口径约定(与 core/trigger_weight_eval.py 完全一致)
--------------------------------------------------
- 前向收益:T+1 开盘买、T+1+H 收盘卖,扣往返成本 0.202%
- 域:20 日均额 >= 8000 万元(tushare ``amount`` 单位千元,阈值 80000)
- 触发面板复用 ``docs/evidence/.cache/trigger_panel.csv``,由
  ``scripts/evaluate_trigger_weight.py --gen-panel`` 逐日重放生产 ``layer4_triggers``
- ``max_bias_200`` 取全局上限(``channel=""``),触发率是下界
- 每日等权后跨日平均;t 值手算(环境无 scipy)
- 本模块只比较**已触发票之间**的排序。路径 B 之上还有 markup +100 / stage 0~15 /
  track 10 / exit -100,面板里复现不了 —— 但那正是路径 B 当判别器的场合:
  非主升候选之间靠它分高下
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

import numpy as np

# 生产分值,与 core/ai_candidate_allocation.py:559-582 逐行对齐。
PROD_POINTS: dict[str, float] = {
    "spring": 12.0,
    "lps": 30.0,
    "evr": 12.0,
    "compression": 22.0,
    "trend_pullback": 34.0,
}
# sos 特殊:单独命中 15,与其它类型共振时整块换成 50。
PROD_SOS_SINGLE = 15.0
PROD_SOS_RESONANT = 50.0
# 拍平臂的统一分值。取 25 是 PROD 六档的中位附近,免得顺带改变「触发 vs 未触发」
# 的相对量级 —— 本模块只比较已触发票之间的排序,统一值取多少不影响排序,
# 取中位只为让 JSON 里的数字可读。
FLAT_POINTS = 25.0

ROUND_TRIP_COST_PCT = 0.202
MIN_AMOUNT_RAW = 80000.0

TRIGGER_KINDS = ("sos", "spring", "lps", "evr", "compression", "trend_pullback")
TOP_N_GRID = (10, 20)
# 置换次数。30 次只能说「在带外」,给不出百分位;200 次的分辨率是 0.5%。
N_PERMUTATIONS = 200
PERMUTATION_SEED = 20260831

MIN_DAYS = 20
MIN_KIND_DAYS = 30
WALK_FORWARD_WARMUP = 60


def tstat(values: list[float]) -> float | None:
    """手工 t 值(环境无 scipy)。方差为零返回 None,不返回 inf。"""
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
    """20260815 -> 20263(年 * 10 + 季)。"""
    year, month = divmod(int(date) // 100, 100)
    return year * 10 + (month - 1) // 3 + 1


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def parse_kinds(kinds: str | float | None) -> frozenset[str]:
    """面板里的 ``"sos|lps"`` -> ``{"sos", "lps"}``。空/缺失 -> 空集。"""
    if kinds is None or (isinstance(kinds, float) and math.isnan(kinds)):
        return frozenset()
    return frozenset(k for k in str(kinds).split("|") if k)


def path_b_score(
    kinds: str | frozenset[str],
    points: dict[str, float],
    *,
    sos_single: float = PROD_SOS_SINGLE,
    sos_resonant: float = PROD_SOS_RESONANT,
) -> float:
    """复现 ``_trigger_score`` 的加总方式。

    与生产逐行对齐:sos 那一档是 ``(50 if other_hits else 15)``,即共振时**整块**
    换成 50 而不是叠加;其余五类各自独立累加。``_signal_weight`` 在未配置权重时
    返回 1.0(``resolve_signal_weight_multiplier``,core/strategy_policy_governor.py:147),
    故这里不乘系数 —— 生产默认路径就是原始分值直接相加。
    """
    ks = kinds if isinstance(kinds, frozenset) else parse_kinds(kinds)
    if not ks:
        return 0.0
    other = ks - {"sos"}
    value = (sos_resonant if other else sos_single) if "sos" in ks else 0.0
    return value + sum(float(points.get(k, 0.0)) for k in other)


def topn_mean(scores: np.ndarray, values: np.ndarray, top_n: int) -> float | None:
    """按分数取前 N 的均值,**边界并列桶按比例分权**。

    这是随机打破并列的期望值,且确定可复。不能用 ``nlargest``:六个分值只组合出
    22 个唯一分数,边界桶中位 17 只票,index 顺序打破并列等于按 ``ts_code``
    字典序选票,会把对照稀释掉(拍平-生产 +0.339 t=+3.58 被读成 +0.131 t=+0.86)。
    """
    n = int(top_n)
    if n <= 0 or len(scores) == 0 or len(scores) != len(values):
        return None
    if len(scores) < n:
        return None
    order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")
    sorted_scores = np.asarray(scores, dtype=float)[order]
    sorted_values = np.asarray(values, dtype=float)[order]
    weights = np.zeros(len(sorted_scores), dtype=float)
    filled = 0.0
    i = 0
    while i < len(sorted_scores) and filled < n:
        j = i
        while j < len(sorted_scores) and sorted_scores[j] == sorted_scores[i]:
            j += 1
        bucket = float(j - i)
        take = min(bucket, n - filled)
        weights[i:j] = take / bucket
        filled += take
        i = j
    total = float(weights.sum())
    if total <= 0:
        return None
    return float(np.dot(weights, sorted_values) / total)


def permutation_tables(
    points: dict[str, float],
    *,
    n_perm: int = N_PERMUTATIONS,
    seed: int = PERMUTATION_SEED,
) -> list[dict[str, float]]:
    """把同一组分值在类别间重排,生成无信息基准表。

    保留分值**集合**不变,只换对应关系 —— 所以基准带回答的是「这六个数字
    分配得对不对」,而不是「六个数字的量级对不对」。生产表若落在带内,说明
    对应关系没带信息;落在带下沿,说明对应关系是反的。
    """
    rng = np.random.default_rng(int(seed))
    keys = list(points.keys())
    vals = [float(points[k]) for k in keys]
    return [dict(zip(keys, rng.permutation(vals), strict=True)) for _ in range(int(n_perm))]


@dataclass
class ArmStat:
    """一条排序臂:按某张分值表选 topN,相对同日流动性域的超额。"""

    label: str
    top_n: int
    days: int
    excess: float | None
    excess_t: float | None
    vs_prod: float | None = None
    vs_prod_t: float | None = None

    @property
    def beats_production(self) -> bool | None:
        """是否显著优于生产表(双侧 5%,且超过往返成本)。"""
        if self.vs_prod is None or self.vs_prod_t is None:
            return None
        return self.vs_prod_t >= 2.0 and self.vs_prod >= ROUND_TRIP_COST_PCT

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "top_n": self.top_n,
            "days": self.days,
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "vs_prod": _round(self.vs_prod),
            "vs_prod_t": _round(self.vs_prod_t, 2),
            "beats_production": self.beats_production,
        }


@dataclass
class PermutationStat:
    """置换检验:生产表在「同样的数字、随机的对应关系」里排第几。"""

    top_n: int
    n_perm: int
    prod: float | None
    band_low: float | None
    band_high: float | None
    band_median: float | None
    percentile: float | None

    @property
    def inside_band(self) -> bool | None:
        if self.prod is None or self.band_low is None or self.band_high is None:
            return None
        return self.band_low <= self.prod <= self.band_high

    @property
    def verdict(self) -> str:
        """百分位读法。

        <=5%   生产接近最差排法 -> 反校准,对应关系是反的
        >=95%  生产接近最好排法 -> 对应关系带正信息
        其余   带内 -> 这六个数字的对应关系不带信息,拍平即可
        """
        if self.percentile is None:
            return "样本不足"
        if self.percentile <= 5.0:
            return f"反校准：生产落在置换带第 {self.percentile:.1f} 百分位（越低越接近最差排法）"
        if self.percentile >= 95.0:
            return f"正校准：生产落在置换带第 {self.percentile:.1f} 百分位"
        return f"无信息：生产落在置换带内（第 {self.percentile:.1f} 百分位），对应关系可拍平"

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_n": self.top_n,
            "n_perm": self.n_perm,
            "prod": _round(self.prod),
            "band_low": _round(self.band_low),
            "band_high": _round(self.band_high),
            "band_median": _round(self.band_median),
            "percentile": _round(self.percentile, 1),
            "inside_band": self.inside_band,
            "verdict": self.verdict,
        }


@dataclass
class KindStat:
    """单个触发类别的生产分值 vs 实测超额。"""

    kind: str
    points: float
    days: int
    excess: float | None
    excess_t: float | None
    rows: int = 0

    @property
    def verdict(self) -> str:
        if self.days < MIN_KIND_DAYS or self.excess is None or self.excess_t is None:
            return "样本不足"
        if self.excess_t <= -2.0:
            return "显著为负"
        if self.excess_t >= 2.0:
            return "显著为正"
        return "不显著"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "points": self.points,
            "days": self.days,
            "rows": self.rows,
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "verdict": self.verdict,
        }


@dataclass
class RankCorrStat:
    """分值排序与实测超额排序的秩相关。负 = 分给反了。"""

    top_n_note: str
    corr: float | None
    n_kinds: int

    @property
    def verdict(self) -> str:
        if self.corr is None or self.n_kinds < 4:
            return "样本不足"
        if self.corr <= -0.30:
            return "分值与超额负相关：高分档反而更差"
        if self.corr >= 0.30:
            return "分值与超额正相关：排序方向对"
        return "近零：分值不携带超额信息"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.top_n_note,
            "corr": _round(self.corr, 3),
            "n_kinds": self.n_kinds,
            "verdict": self.verdict,
        }


@dataclass
class SosResonanceStat:
    """sos 共振加成(15 -> 50)是否成立。全表最大的一次加分,单独验。"""

    single_excess: float | None
    single_t: float | None
    single_rows: int
    resonant_excess: float | None
    resonant_t: float | None
    resonant_rows: int

    @property
    def gap(self) -> float | None:
        """共振 - 单独。加成成立的话应为正。"""
        if self.single_excess is None or self.resonant_excess is None:
            return None
        return self.resonant_excess - self.single_excess

    @property
    def verdict(self) -> str:
        if self.resonant_rows < 200 or self.gap is None:
            return "样本不足"
        if self.gap > 0:
            return f"共振优于单独 {self.gap:+.3f}：加成方向成立"
        return f"共振**差于**单独 {self.gap:+.3f}：15 -> 50 的加成方向是反的"

    def as_dict(self) -> dict[str, Any]:
        return {
            "single": {
                "points": PROD_SOS_SINGLE,
                "rows": self.single_rows,
                "excess": _round(self.single_excess),
                "excess_t": _round(self.single_t, 2),
            },
            "resonant": {
                "points": PROD_SOS_RESONANT,
                "rows": self.resonant_rows,
                "excess": _round(self.resonant_excess),
                "excess_t": _round(self.resonant_t, 2),
            },
            "gap": _round(self.gap),
            "verdict": self.verdict,
        }


@dataclass
class WalkForwardStat:
    """走前挑分值表 vs 固定生产表。这一格是上线依据,消融不能替。"""

    top_n: int
    days: int
    chosen: float | None
    fixed: float | None
    diff: float | None
    diff_t: float | None
    pick_dist: dict[str, float] = field(default_factory=dict)

    @property
    def is_concentrated(self) -> bool | None:
        """选中是否集中在单一候选表上(>=80%)。"""
        if not self.pick_dist:
            return None
        return max(self.pick_dist.values()) >= 0.80

    @property
    def top_pick(self) -> str | None:
        if not self.pick_dist:
            return None
        return max(self.pick_dist.items(), key=lambda kv: kv[1])[0]

    @property
    def picks_off_production(self) -> float | None:
        """选中**非生产表**的比例。"""
        if not self.pick_dist:
            return None
        return sum(v for k, v in self.pick_dist.items() if k != "prod")

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t < 2.0:
            return "走前不显著：不足以支持改动"
        off = self.picks_off_production
        if off is not None and off >= 0.80:
            if self.is_concentrated:
                return f"走前显著且选中集中于 {self.top_pick}：支持替换生产分值表"
            return "走前显著、选中均非生产表但候选散开：方向成立，具体换成哪张待定"
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
            "pick_dist": {k: _round(v, 3) for k, v in sorted(self.pick_dist.items())},
            "is_concentrated": self.is_concentrated,
            "top_pick": self.top_pick,
            "picks_off_production": _round(self.picks_off_production, 3),
            "verdict": self.verdict,
        }


@dataclass
class QuarterStat:
    """按季度切拍平-生产的增量,看结论在不同行情段是否稳。"""

    quarter: int
    days: int
    diff: float | None
    diff_t: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "quarter": self.quarter,
            "days": self.days,
            "diff": _round(self.diff),
            "diff_t": _round(self.diff_t, 2),
        }


@dataclass
class PointsReport:
    arms: list[ArmStat] = field(default_factory=list)
    permutations: list[PermutationStat] = field(default_factory=list)
    kinds: list[KindStat] = field(default_factory=list)
    rank_corr: RankCorrStat | None = None
    sos: SosResonanceStat | None = None
    walk_forward: list[WalkForwardStat] = field(default_factory=list)
    quarters: list[QuarterStat] = field(default_factory=list)
    tie_bucket_median: int | None = None
    tie_bucket_max: int | None = None
    unique_scores: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "arms": [a.as_dict() for a in self.arms],
            "permutation": [p.as_dict() for p in self.permutations],
            "kinds": [k.as_dict() for k in self.kinds],
            "rank_corr": self.rank_corr.as_dict() if self.rank_corr else {},
            "sos_resonance": self.sos.as_dict() if self.sos else {},
            "walk_forward": [w.as_dict() for w in self.walk_forward],
            "quarters": [q.as_dict() for q in self.quarters],
            "tie_break": {
                "unique_scores": self.unique_scores,
                "boundary_bucket_median": self.tie_bucket_median,
                "boundary_bucket_max": self.tie_bucket_max,
                "note": (
                    "六个分值只组合出少量唯一分数，topN 边界并列桶很宽。本模块按比例分权"
                    "（= 随机打破并列的期望），不用 nlargest —— 后者按 ts_code 顺序选票，"
                    "会把对照稀释掉（拍平-生产 +0.339 t=+3.58 被读成 +0.131 t=+0.86）。"
                ),
            },
            "production": {
                "points": dict(PROD_POINTS),
                "sos_single": PROD_SOS_SINGLE,
                "sos_resonant": PROD_SOS_RESONANT,
                "flat_points": FLAT_POINTS,
                "source": "core/ai_candidate_allocation.py:559-582",
            },
            "cost_threshold_pct": ROUND_TRIP_COST_PCT,
            "reading": (
                "excess 为相对同日流动性域内全体的超额，只在**已触发票之间**比排序。"
                "置换带保留分值集合不变、只换对应关系，故它回答「这六个数字分配得对不对」，"
                "不回答「量级对不对」。生产落在带下沿 = 反校准。改生产分值仍须三闸全过："
                "替代表显著优于生产 + 走前 t>=2 + 选中集中。"
            ),
        }


def summarize_arm(label: str, top_n: int, arm: list[float], prod: list[float] | None = None) -> ArmStat:
    """一条臂的超额与 t 值;给了 prod 就同时算同日配对差。

    配对差用同日相减而不是两组均值相减 —— 两条臂看的是同一批交易日,配对能消掉
    日级别的共同波动,t 值才不被大盘噪声吃掉。
    """
    clean = [float(v) for v in arm if v is not None and math.isfinite(float(v))]
    stat = ArmStat(
        label=label,
        top_n=top_n,
        days=len(clean),
        excess=mean(clean) if clean else None,
        excess_t=tstat(clean),
    )
    if prod is not None and len(prod) == len(arm):
        pairs = [
            float(a) - float(b)
            for a, b in zip(arm, prod, strict=True)
            if a is not None and b is not None and math.isfinite(float(a)) and math.isfinite(float(b))
        ]
        if pairs:
            stat.vs_prod = mean(pairs)
            stat.vs_prod_t = tstat(pairs)
    return stat


def summarize_permutation(top_n: int, prod_mean: float | None, perm_means: list[float]) -> PermutationStat:
    """生产表在置换分布里的百分位。

    百分位定义为「不优于生产的置换占比」,含并列。生产表的 12.0 出现两次
    (spring/evr),必然有若干置换与生产同构、数值完全相同,所以百分位下限不是 0
    而是这些同构表的占比 —— 这也是「不存在比生产更差的排法」的表现形式。
    """
    clean = [float(v) for v in perm_means if v is not None and math.isfinite(float(v))]
    if prod_mean is None or len(clean) < 10:
        return PermutationStat(top_n, len(clean), _round(prod_mean), None, None, None, None)
    arr = np.asarray(clean, dtype=float)
    pct = float((arr <= float(prod_mean)).mean() * 100.0)
    return PermutationStat(
        top_n=top_n,
        n_perm=len(clean),
        prod=float(prod_mean),
        band_low=float(arr.min()),
        band_high=float(arr.max()),
        band_median=float(np.median(arr)),
        percentile=pct,
    )


def summarize_kind(kind: str, points: float, daily: list[float], rows: int = 0) -> KindStat:
    clean = [float(v) for v in daily if v is not None and math.isfinite(float(v))]
    return KindStat(
        kind=kind,
        points=float(points),
        days=len(clean),
        excess=mean(clean) if clean else None,
        excess_t=tstat(clean),
        rows=int(rows),
    )


def rank_correlation(kinds: list[KindStat]) -> RankCorrStat:
    """分值 vs 超额的 Spearman(环境无 scipy,用秩上的 Pearson)。"""
    usable = [k for k in kinds if k.excess is not None and k.days >= MIN_KIND_DAYS]
    if len(usable) < 4:
        return RankCorrStat("单类型命中", None, len(usable))
    pts = _ranks([k.points for k in usable])
    exc = _ranks([float(k.excess) for k in usable])  # type: ignore[arg-type]
    corr = float(np.corrcoef(pts, exc)[0, 1]) if len(usable) > 1 else None
    if corr is not None and not math.isfinite(corr):
        corr = None
    return RankCorrStat("单类型命中", corr, len(usable))


def _ranks(values: list[float]) -> np.ndarray:
    """平均秩(并列取均值),与 pandas ``rank(method="average")`` 一致。"""
    arr = np.asarray(values, dtype=float)
    order = np.argsort(arr, kind="stable")
    ranks = np.empty(len(arr), dtype=float)
    i = 0
    while i < len(arr):
        j = i
        while j < len(arr) and arr[order[j]] == arr[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0
        i = j
    return ranks


def summarize_quarters(dates: list[int], diffs: list[float]) -> list[QuarterStat]:
    """按季度切增量。两端的季度往往不满,读的时候看 days。"""
    buckets: dict[int, list[float]] = {}
    for date, diff in zip(dates, diffs, strict=True):
        if diff is None or not math.isfinite(float(diff)):
            continue
        buckets.setdefault(quarter_of(int(date)), []).append(float(diff))
    return [
        QuarterStat(quarter=q, days=len(v), diff=mean(v) if v else None, diff_t=tstat(v))
        for q, v in sorted(buckets.items())
    ]


def walk_forward_table(
    top_n: int,
    dates: list[int],
    by_table: dict[str, list[float]],
    *,
    horizon: int,
    warmup: int = WALK_FORWARD_WARMUP,
) -> WalkForwardStat:
    """走前挑分值表:T 日只用**已结算**的历史(截到 T-H-1),再跟固定生产表比。

    截到 T-H-1 而非 T-1 是必须的:T-H..T-1 的前向收益在 T 日还没结算,用了就是
    未来信息。候选表由调用方给(生产/拍平/按超额重排/去共振...),键 ``"prod"``
    必须在内。
    """
    lag = int(horizon) + 1
    if not by_table or "prod" not in by_table:
        return WalkForwardStat(top_n, 0, None, None, None, None, {})
    chosen: list[float] = []
    fixed: list[float] = []
    picks: list[str] = []
    for i in range(int(warmup), len(dates)):
        end = i - lag
        if end <= MIN_DAYS:
            continue
        best_key, best_val = "prod", -math.inf
        for key, series in by_table.items():
            hist = [v for v in series[:end] if v is not None and math.isfinite(float(v))]
            if not hist:
                continue
            value = sum(hist) / len(hist)
            if value > best_val:
                best_key, best_val = key, value
        pick = by_table[best_key][i]
        base = by_table["prod"][i]
        if pick is None or base is None or not math.isfinite(float(pick)) or not math.isfinite(float(base)):
            continue
        chosen.append(float(pick))
        fixed.append(float(base))
        picks.append(best_key)
    if len(chosen) < MIN_DAYS:
        return WalkForwardStat(top_n, len(chosen), None, None, None, None, {})
    diffs = [c - f for c, f in zip(chosen, fixed, strict=True)]
    dist: dict[str, int] = {}
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


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _double_count_section() -> list[str]:
    """先讲清路径 A / 路径 B,否则读者会以为这轮在重复前两轮。"""
    return [
        "### 同一个触发信号被记了两次",
        "",
        _row(["路径", "入口", "全摆幅（最终分）"]),
        _row(["---"] * 3),
        _row(["A `trigger_q`", "`watch_score` 0.30 → `_layer3_rank_bonus`", "2.4 分"]),
        _row(["B `_trigger_score`", "`ai_candidate_allocation.py:518` 直接加", "12 ~ 50 分"]),
        "",
        "前两轮（dry_q / trigger_q）审的是路径 A。一次 lps 命中（30 分）等于 "
        "`trigger_q` 整个摆幅的 12.5 倍，所以真正决定「已触发的票谁排前面」的是路径 B。",
        "",
        "**两条路径不可同时下调** —— 同一个信号被罚两次，净效果会超过任一轮量到的幅度。"
        "路径 A 那轮结论是维持，本轮只议路径 B。",
        "",
    ]


def _tie_section(report: PointsReport) -> list[str]:
    if report.unique_scores is None:
        return []
    return [
        "### 并列打破方式会改变结论强度",
        "",
        f"六个分值只组合出 **{report.unique_scores}** 个唯一分数，每日 topN 的边界并列桶"
        f"中位 **{report.tie_bucket_median}** 只（最大 {report.tie_bucket_max}）。",
        "",
        "`nlargest` 按 index 顺序打破并列，index 跟着 `ts_code` 排、`ts_code` 又与交易所"
        "相关 —— 等于偷偷按代码字典序选票。本模块对边界桶**按比例分权**（= 随机打破并列"
        "的期望值，且确定可复）。实测差别：拍平-生产 `nlargest` 读成 +0.131 (t=+0.86)，"
        "分权后是 +0.339 (t=+3.58)。",
        "",
    ]


def _arm_table(arms: list[ArmStat]) -> list[str]:
    lines = ["### 排序臂对照（只在已触发票之间比）", ""]
    if not arms:
        return lines + ["样本不足。", ""]
    lines += [
        _row(["臂", "topN", "天数", "超额", "t", "vs 生产", "t", "显著优于生产"]),
        _row(["---"] * 8),
    ]
    for arm in arms:
        beats = arm.beats_production
        mark = "—" if beats is None else ("是" if beats else "否")
        lines.append(
            _row(
                [
                    arm.label,
                    str(arm.top_n),
                    str(arm.days),
                    _signed(arm.excess),
                    _plain(arm.excess_t),
                    _signed(arm.vs_prod),
                    _plain(arm.vs_prod_t),
                    mark,
                ]
            )
        )
    lines += [
        "",
        "所有臂都是负超额 —— 触发这一层信号本身在样本里是负的（路径 A 那轮已证）。"
        "本表问的不是「触发好不好」，是**在已触发的票里，这六个分值排得对不对**。",
        "",
    ]
    return lines


def _permutation_table(stats: list[PermutationStat]) -> list[str]:
    lines = ["### 置换检验：生产分值 vs 随机重排同一组数字", ""]
    if not stats:
        return lines + ["样本不足。", ""]
    lines += [
        _row(["topN", "置换次数", "生产", "带下沿", "带中位", "带上沿", "百分位", "判定"]),
        _row(["---"] * 8),
    ]
    for stat in stats:
        lines.append(
            _row(
                [
                    str(stat.top_n),
                    str(stat.n_perm),
                    _signed(stat.prod),
                    _signed(stat.band_low),
                    _signed(stat.band_median),
                    _signed(stat.band_high),
                    "—" if stat.percentile is None else f"{stat.percentile:.1f}%",
                    stat.verdict.split("：")[0],
                ]
            )
        )
    lines += [
        "",
        "置换保留分值**集合**不变、只换与类别的对应关系，所以这一格回答「这六个数字"
        "分配得对不对」，不回答「量级对不对」。落在带内 = 对应关系不带信息（拍平即可）；"
        "落在带下沿 = 反校准，对应关系是反的。",
        "",
    ]
    return lines


def _kind_table(kinds: list[KindStat], corr: RankCorrStat | None) -> list[str]:
    lines = ["### 生产分值 vs 实测超额（单类型命中）", ""]
    if not kinds:
        return lines + ["样本不足。", ""]
    lines += [
        _row(["触发类型", "生产分", "行数", "天数", "超额", "t", "判定"]),
        _row(["---"] * 7),
    ]
    for kind in sorted(kinds, key=lambda k: -k.points):
        lines.append(
            _row(
                [
                    kind.kind,
                    _plain(kind.points, 1),
                    str(kind.rows),
                    str(kind.days),
                    _signed(kind.excess),
                    _plain(kind.excess_t),
                    kind.verdict,
                ]
            )
        )
    if corr is not None:
        lines += ["", f"分值 vs 超额秩相关 **{_signed(corr.corr)}** —— {corr.verdict}。"]
    lines.append("")
    return lines


def _sos_section(sos: SosResonanceStat | None) -> list[str]:
    lines = ["### sos 共振加成（15 → 50）单独验", ""]
    if sos is None:
        return lines + ["样本不足。", ""]
    lines += [
        _row(["档", "生产分", "行数", "超额", "t"]),
        _row(["---"] * 5),
        _row(
            [
                "sos 单独",
                _plain(PROD_SOS_SINGLE, 1),
                str(sos.single_rows),
                _signed(sos.single_excess),
                _plain(sos.single_t),
            ]
        ),
        _row(
            [
                "sos 共振",
                _plain(PROD_SOS_RESONANT, 1),
                str(sos.resonant_rows),
                _signed(sos.resonant_excess),
                _plain(sos.resonant_t),
            ]
        ),
        "",
        f"{sos.verdict}。这是全表最大的一次加分（+35 分，比第二大的 trend_pullback 整档还多），"
        "所以单独立一格，不混在类型表里。",
        "",
    ]
    return lines


def _walk_forward_table(stats: list[WalkForwardStat]) -> list[str]:
    lines = ["### 走前挑分值表（上线依据，消融不能替）", ""]
    if not stats:
        return lines + ["样本不足。", ""]
    lines += [
        _row(["topN", "天数", "走前选中", "固定生产", "增量", "t", "选中分布", "判定"]),
        _row(["---"] * 8),
    ]
    for stat in stats:
        dist = "、".join(f"{k} {v * 100:.0f}%" for k, v in sorted(stat.pick_dist.items(), key=lambda kv: -kv[1]))
        lines.append(
            _row(
                [
                    str(stat.top_n),
                    str(stat.days),
                    _signed(stat.chosen),
                    _signed(stat.fixed),
                    _signed(stat.diff),
                    _plain(stat.diff_t),
                    dist or "—",
                    stat.verdict,
                ]
            )
        )
    lines += [
        "",
        "T 日只用截到 T-H-1 的**已结算**历史挑表（T-H..T-1 的前向收益在 T 日还没结算，"
        "用了就是未来信息）。消融测「这一项好不好」，走前测「换一张能不能真的更好」，"
        "后者才是上线依据。",
        "",
    ]
    return lines


def _quarter_table(quarters: list[QuarterStat]) -> list[str]:
    lines = ["### 按季度切（拍平 - 生产）", ""]
    if not quarters:
        return lines + ["样本不足。", ""]
    lines += [_row(["季度", "天数", "增量", "t"]), _row(["---"] * 4)]
    for q in quarters:
        lines.append(_row([str(q.quarter), str(q.days), _signed(q.diff), _plain(q.diff_t)]))
    positives = [q for q in quarters if q.diff is not None and q.diff > 0]
    lines += [
        "",
        f"{len(positives)}/{len(quarters)} 个季度拍平优于生产。两端季度往往不满，看 days 再读。",
        "",
    ]
    return lines


def decision(report: PointsReport) -> list[str]:
    """把置换 / 类型 / 共振 / 走前收成可执行结论。

    分级判据与前两轮一致：结论若指向**改生产参数**，要求消融显著 + 走前 t>=2 +
    选中集中三闸全过。反校准是个强信号，但它属于第 1 闸，不能替第 2 闸 ——
    「生产排法是反的」和「换成这张表会更好」是两个问题。
    """
    lines: list[str] = []
    lines.append(_permutation_finding(report.permutations))
    lines.append(_rank_finding(report.kinds, report.rank_corr))
    lines.append(_sos_finding(report.sos))
    lines.append(_points_action(report.arms, report.walk_forward))
    lines.append(
        f"⑤ 任何分值改动落地前：幅度须大于单次往返成本 {ROUND_TRIP_COST_PCT}%、"
        "跨行情段方向稳定，且**不与路径 A（`trigger_q` 0.30）同时下调** —— "
        "同一个信号被罚两次，净效果会超过任一轮量到的幅度。"
    )
    return lines


def _permutation_finding(stats: list[PermutationStat]) -> str:
    ready = [s for s in stats if s.percentile is not None]
    if not ready:
        return "① 置换检验：样本不足。"
    anti = [s for s in ready if s.percentile is not None and s.percentile <= 5.0]
    inside = [s for s in ready if s.inside_band]
    if len(anti) == len(ready):
        cells = "、".join(f"top{s.top_n} 第 {s.percentile:.1f} 百分位" for s in anti)
        return (
            f"① **反校准**：{cells}，生产在 {ready[0].n_perm} 次置换里接近最差排法。"
            "不是「校准不准」，是这六个数字的对应关系给反了。"
        )
    if anti:
        cells = "、".join(f"top{s.top_n}" for s in anti)
        return f"① 部分反校准：{cells} 落在置换带下沿 5% 内，其余格在带内。集中端成立、分散端不成立。"
    if len(inside) == len(ready):
        return "① 置换带内：这六个数字的**对应关系不带信息**，拍平即可，不必逐个调。"
    return "① 置换结果混杂：不同 topN 指向不同方向，先补样本。"


def _rank_finding(kinds: list[KindStat], corr: RankCorrStat | None) -> str:
    ready = [k for k in kinds if k.days >= MIN_KIND_DAYS and k.excess is not None]
    if not ready or corr is None or corr.corr is None:
        return "② 分值 vs 超额：样本不足。"
    highest = max(ready, key=lambda k: k.points)
    lowest = min(ready, key=lambda k: k.points)
    detail = (
        f"最高分档 {highest.kind}({highest.points:.0f}) 超额 {_signed(highest.excess)}、"
        f"最低分档 {lowest.kind}({lowest.points:.0f}) 超额 {_signed(lowest.excess)}"
    )
    if corr.corr <= -0.30:
        return f"② 秩相关 {_signed(corr.corr)}：**高分档反而更差**。{detail}。"
    if corr.corr >= 0.30:
        return f"② 秩相关 {_signed(corr.corr)}：分值方向对，不必重排。{detail}。"
    return f"② 秩相关 {_signed(corr.corr)} 近零：分值不携带超额信息。{detail}。"


def _sos_finding(sos: SosResonanceStat | None) -> str:
    if sos is None or sos.gap is None or sos.resonant_rows < 200:
        return "③ sos 共振加成：样本不足（共振命中本就稀少）。"
    if sos.gap > 0:
        return f"③ sos 共振优于单独 {_signed(sos.gap)}：15 → 50 的加成方向成立，本项不动。"
    return (
        f"③ sos 共振**差于**单独 {_signed(sos.gap)}（共振 {sos.resonant_rows} 行）："
        "全表最大的一次加分（+35）方向是反的，与「多信号共振更可靠」的直觉相反 —— "
        "和路径 A 那轮查到的 `n_hits` 分布（共振几乎不存在）是同一件事。"
    )


def _points_action(arms: list[ArmStat], walk_forward: list[WalkForwardStat]) -> str:
    """④ 是否动生产分值。三道闸全过才给「支持替换」。"""
    ready_arms = [a for a in arms if a.vs_prod_t is not None and a.days >= MIN_DAYS]
    ready_wf = [w for w in walk_forward if w.days >= MIN_DAYS and w.diff_t is not None]
    if not ready_arms or not ready_wf:
        return "④ 生产分值维持原样：样本不足，先补数据。"
    beats = [a for a in ready_arms if a.beats_production]
    wf_pass = [w for w in ready_wf if w.diff_t is not None and w.diff_t >= 2.0 and w.is_concentrated]
    if beats and wf_pass:
        names = "、".join(sorted({a.label for a in beats}))
        picks = "、".join(f"top{w.top_n}→{w.top_pick}" for w in wf_pass)
        return (
            f"④ **支持替换生产分值表**：{names} 显著优于生产，且走前挑表通过（{picks}）。"
            "三道闸全过。落地前按 ⑤ 复核成本与路径 A 的交互。"
        )
    if not beats:
        return (
            "④ 生产分值维持原样：替代表未显著优于生产（未同时满足 t>=2 与超过成本"
            f" {ROUND_TRIP_COST_PCT}%）。反校准只说明排法可疑，不足以指定替换成哪张。"
        )
    near = [w for w in ready_wf if w.diff_t is not None and 1.80 <= w.diff_t < 2.0]
    tail = ""
    if near:
        cells = "、".join(f"top{w.top_n} t={w.diff_t:+.2f}" for w in near)
        # t=1.96 是双侧 5% 的临界点，差 0.04 就四舍五入放行的话，这道闸等于没有。
        tail = f"（{cells} 贴着线但没过，不四舍五入；样本再长一点自己会说话）"
    return (
        f"④ 生产分值暂维持：消融那格已过（{len(beats)}/{len(ready_arms)} 条臂显著优于生产），"
        f"但走前挑表未通过（t<2 或选中未集中）{tail}。"
        "消融测「这一项好不好」，走前测「换一张能不能真的更好」，后者才是上线依据。"
    )


def render(report: PointsReport, *, horizon: int, start: int, end: int) -> str:
    days = f"{start}..{end}"
    lines = [
        f"## 触发分值体检（`_trigger_score` 路径 B）｜T+{horizon}｜{days}",
        "",
        f"审的是 `core/ai_candidate_allocation.py:559-582` 那六个硬编码分值"
        f"（{PROD_SOS_SINGLE:.0f}~{PROD_SOS_RESONANT:.0f}），不是 `watch_score` 里的权重。",
        "",
    ]
    lines += _double_count_section()
    lines += _arm_table(report.arms)
    lines += _permutation_table(report.permutations)
    lines += _tie_section(report)
    lines += _kind_table(report.kinds, report.rank_corr)
    lines += _sos_section(report.sos)
    lines += _walk_forward_table(report.walk_forward)
    lines += _quarter_table(report.quarters)
    lines += ["### 结论", ""]
    lines += [f"{line}" for line in decision(report)]
    lines += [
        "",
        f"口径：T+1 开盘买 / T+1+{horizon} 收盘卖，扣往返成本 {ROUND_TRIP_COST_PCT}%；"
        "域为 20 日均额 >= 8000 万元；每日等权后跨日平均；t 值手算（环境无 scipy）。",
    ]
    return "\n".join(lines)
