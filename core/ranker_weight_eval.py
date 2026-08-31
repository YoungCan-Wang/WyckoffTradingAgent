"""排序权重体检：``watch_score`` 里的 ``dry_q``（生产 0.20）值不值这个分量。

2026-08-31 首轮跑了 368 个交易日（有效区间 20250212..20260813，原始面板
20250102..20260828 共 402 日，前 25 日预热、后 H+1 日待结算被自然剔除）全市场，
结论是 **dry_q 保留，权重不动**。它不是配平项，而是一个独立选择器——去掉它
以后，纯动量端在流动性域内是**负超额**：

    H=10 top10   留 dry_q +0.420(t=+0.94)   去 dry_q -0.752(t=-1.74)   配对差 +1.173(t=+3.12)
    H=10 top20   留       +0.066(t=+0.17)   去       -0.525(t=-1.31)   配对差 +0.591(t=+2.11)
    H=5  top10   留       +0.393(t=+1.20)   去       -0.528(t=-1.55)   配对差 +0.921(t=+3.02)
    H=5  top20   留       +0.078(t=+0.27)   去       -0.548(t=-1.85)   配对差 +0.626(t=+2.86)

四格全部 t>2、全部高于单次往返成本 0.202%、7 个季度里 5~6 个为正。两臂 topN
重合度只有 9~12%，说明它换掉的是绝大部分选择结果,不是排序末端的微调。

三个具体发现——

**一、无条件分档是驼峰,不是单调。** 域内按 dry_q 五档看 H=10 超额：最湿
-0.204(t=-3.05)、偏湿 +0.160(t=+4.82)、中档 +0.146(t=+6.10)、偏干
-0.029(t=-0.81)、最干 -0.073(t=-1.22)。**峰在中间两档，最干的一档并不最好**。
H=5 更极端：最干 -0.090(t=-2.13)、偏干 -0.058(t=-2.13) 都显著为负。所以
dry_q 的收益来自「躲开放量」，而不是「越缩量越好」——把它当单调因子用是错的。

**二、固定动量后干湿差几乎为零。** 按 ret20 五档配对，档内最干减最湿只有
-0.037pct（t=-0.42）。单看这一条会以为 dry_q 无用。它与发现一并不矛盾：
dry_q 起作用的方式是**改变在动量维度上的落点**，而不是在同一动量档里挑票。
把它当「动量档内的二次排序」来测，会测不到它的贡献。

**三、动态调权重不可上线,原因是曲线平。** 在 {0,0.2,0.4,0.6,0.8,1.2,2.0}
上，H=10 top10 超额是 -1.134 / +0.038 / +0.309 / +0.163 / +0.227 / +0.103 / +0.095
——只有 w=0 明显更差，0.20 往后就平了、没有单调延续。走前挑权重时选中分布散在
0.40(57%)/0.80(21%)/1.20(19%) 之间，差值 t 只有 +0.70（H=5 为 +0.76）。
**窄网格（上界 0.40）里「走前总选 0.40」是网格边界造成的假象**；网格放宽后
就散开了，这是平坦目标被拟合到噪声上的特征。

所以首轮**只确认保留，不动权重**：0 → 0.20 这一步 t>2 且四格一致，
0.20 → 0.40 这一步 t<2 且宽网格下不成形。

口径约定
--------
- 前向收益：T+1 开盘买、T+1+H 收盘卖，扣单次往返成本 0.202%
- 域：20 日均额 >= 8000 万元（tushare ``amount`` 单位为千元，故阈值取 80000）
- 特征只用到 T 日及之前的 bar，与 core/candidate_ranker.py 同式：
  ``vol_ratio = volume / volume.rolling(20).mean()``，再取尾 5 根的最小值
- 打分臂含 ``extension_penalty``（ret20>45% 扣至多 0.30、ret5>18% 扣至多 0.10），
  否则基准臂比生产更天真，会把 dry_q 的贡献算大
- 每日等权后再跨日平均；``excess`` 为相对同日流动性域内全体的超额
- t 值为手工计算（环境无 scipy）

随机负控制是必需的
------------------
给动量分加**任何**一个 0.20 权重的搅动项都会把选择从动量极端端拉开，本身就
值几毛钱。所以「加了 dry_q 比纯动量好」不构成证据，必须和同权重随机分位比。
``rand_band`` 就是这个无信息基准；首轮 dry_q 在四格里三格落在带外。

同理，**比法本身会改变结论**：按「相对基准臂的增量」比，随机带宽宽到能吞掉
dry_q；按「相对同日域内的超额」比，dry_q 在带外。后者才是对的口径——前者
把两个都含噪的臂相减，等于把噪声算了两遍。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

# 生产值，用于在报告里标注「当前档位」。
PROD_DRY_WEIGHT = 0.20
PROD_Q20_WEIGHT = 0.25
PROD_Q5_WEIGHT = 0.20
PROD_Q3_WEIGHT = 0.05
PROD_EXT_RET20_MIN = 45.0
PROD_EXT_RET20_SPAN = 55.0
PROD_EXT_RET20_MAX = 0.30
PROD_EXT_RET5_MIN = 18.0
PROD_EXT_RET5_SPAN = 22.0
PROD_EXT_RET5_MAX = 0.10

ROUND_TRIP_COST_PCT = 0.202
MIN_AMOUNT_RAW = 80000.0

# 宽网格。首轮的教训是窄网格（上界 0.40）会让走前「总选边界值」，读成单调向上。
WEIGHT_GRID = (0.0, 0.20, 0.40, 0.60, 0.80, 1.20, 2.00)
DRY_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.2, "最湿20%"),
    (0.2, 0.4, "偏湿"),
    (0.4, 0.6, "中档"),
    (0.6, 0.8, "偏干"),
    (0.8, 1.0, "最干20%"),
)
TOP_N_GRID = (10, 20)
RANDOM_SEEDS = (1, 2, 3, 4, 5)

MIN_DAYS = 20
MIN_GROUP = 20
MIN_BAND_SIZE = 5


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

    基准臂必须带上它。不带的话基准臂比生产更天真，dry_q 的贡献会被算大
    （首轮不带 ext 时 top10 增量 +1.506，带上后 +1.173）。
    """
    p20 = min(max((float(ret20) - PROD_EXT_RET20_MIN) / PROD_EXT_RET20_SPAN, 0.0), 1.0) * PROD_EXT_RET20_MAX
    p5 = min(max((float(ret5) - PROD_EXT_RET5_MIN) / PROD_EXT_RET5_SPAN, 0.0), 1.0) * PROD_EXT_RET5_MAX
    return p20 + p5


def band_of(dry_pct: float) -> str | None:
    """dry_q 分位归档。分位取值应在 [0,1]，越界返回 None。"""
    value = float(dry_pct)
    if not 0.0 <= value <= 1.0:
        return None
    for low, high, label in DRY_BANDS:
        if low <= value < high or (high >= 1.0 and value >= low):
            return label
    return None


@dataclass
class BandStat:
    """dry_q 一档的域内超额。"""

    label: str
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
            return "不显著"
        return "显著为正" if self.excess > 0 else "显著为负"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "avg_size": _round(self.avg_size, 1),
            "inside": _round(self.inside),
            "inside_t": _round(self.inside_t, 2),
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "verdict": self.verdict,
        }


@dataclass
class AblationStat:
    """留 dry_q vs 去 dry_q 的逐日配对差值,外加同权重随机臂的带宽。"""

    top_n: int
    days: int
    keep: float | None
    keep_t: float | None
    drop: float | None
    drop_t: float | None
    diff: float | None
    diff_t: float | None
    overlap: float | None
    rand_min: float | None
    rand_max: float | None
    excess_by_quarter: dict[int, float] = field(default_factory=dict)

    @property
    def beats_random(self) -> bool | None:
        if self.diff is None or self.rand_min is None or self.rand_max is None:
            return None
        return not (self.rand_min <= self.diff <= self.rand_max)

    @property
    def positive_quarters(self) -> str:
        if not self.excess_by_quarter:
            return "—"
        pos = sum(1 for v in self.excess_by_quarter.values() if v > 0)
        return f"{pos}/{len(self.excess_by_quarter)}"

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t < 2.0:
            return "不显著：不足以支持保留"
        if self.diff <= ROUND_TRIP_COST_PCT:
            return "显著但不抵成本"
        if self.beats_random is False:
            return "落在随机带内：不可归因"
        return "显著且抵成本：支持保留"

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
            "overlap": _round(self.overlap, 3),
            "rand_min": _round(self.rand_min),
            "rand_max": _round(self.rand_max),
            "beats_random": self.beats_random,
            "positive_quarters": self.positive_quarters,
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
        return abs(self.weight - PROD_DRY_WEIGHT) < 1e-9

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
        """选中分布是否集中在单一权重上。散开说明目标平坦、在拟合噪声。"""
        if not self.pick_dist:
            return None
        return max(self.pick_dist.values()) >= 0.80

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t < 2.0:
            reason = "" if self.is_concentrated else "（且选中分布散开，目标函数平坦）"
            return f"走前不显著：不可上线{reason}"
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
            "verdict": self.verdict,
        }


@dataclass
class RankerReport:
    bands: list[BandStat] = field(default_factory=list)
    ablation: list[AblationStat] = field(default_factory=list)
    weights: dict[int, list[WeightStat]] = field(default_factory=dict)
    walk_forward: list[WalkForwardStat] = field(default_factory=list)
    matched_spread: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_bands": [b.as_dict() for b in self.bands],
            "ablation": [a.as_dict() for a in self.ablation],
            "weight_grid": {str(k): [w.as_dict() for w in v] for k, v in sorted(self.weights.items())},
            "walk_forward": [w.as_dict() for w in self.walk_forward],
            "momentum_matched_spread": self.matched_spread,
            "production": {
                "dry_weight": PROD_DRY_WEIGHT,
                "q20_weight": PROD_Q20_WEIGHT,
                "q5_weight": PROD_Q5_WEIGHT,
                "q3_weight": PROD_Q3_WEIGHT,
            },
            "cost_threshold_pct": ROUND_TRIP_COST_PCT,
            "reading": (
                "excess 为相对同日流动性域内全体的超额。消融的 diff 为「留 dry_q 减去 dry_q」"
                "的逐日配对差值，为正说明该项有贡献；须同时高于成本 0.202% 且落在随机带之外。"
                "分档看的是无条件分位，驼峰形（最干档非最优）说明收益来自躲开放量而非越缩越好。"
            ),
        }


def summarize_band(label: str, daily: list[dict[str, float]]) -> BandStat:
    """把某一档的逐日观测汇总。每日等权，避免入选只数多的日子主导均值。"""
    usable = [r for r in daily if r.get("inside") is not None and r.get("domain") is not None]
    if len(usable) < MIN_DAYS:
        return BandStat(label, len(usable), 0.0, None, None, None, None)
    ins = [float(r["inside"]) for r in usable]
    exc = [float(r["inside"]) - float(r["domain"]) for r in usable]
    return BandStat(
        label=label,
        days=len(usable),
        avg_size=mean(float(r.get("size") or 0) for r in usable),
        inside=mean(ins),
        inside_t=tstat(ins),
        excess=mean(exc),
        excess_t=tstat(exc),
    )


def summarize_ablation(
    top_n: int,
    rows: list[dict[str, float]],
    rand_diffs: list[float] | None = None,
) -> AblationStat:
    """逐日配对：同日同域同动量分，只差 dry_q 一项。配对能消掉市场共同成分。"""
    usable = [r for r in rows if r.get("keep") is not None and r.get("drop") is not None]
    if len(usable) < MIN_DAYS:
        return AblationStat(top_n, len(usable), None, None, None, None, None, None, None, None, {})
    keep = [float(r["keep"]) for r in usable]
    drop = [float(r["drop"]) for r in usable]
    diff = [k - d for k, d in zip(keep, drop, strict=True)]
    by_q: dict[int, list[float]] = {}
    for row, value in zip(usable, diff, strict=True):
        by_q.setdefault(quarter_of(int(row["date"])), []).append(value)
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


def walk_forward_weight(
    top_n: int,
    dates: list[int],
    by_weight: dict[float, list[float]],
    *,
    horizon: int,
    warmup: int = 120,
) -> WalkForwardStat:
    """走前挑权重：T 日只用**已结算**的历史（截到 T-H-1），再跟固定生产权重比。

    截到 T-H-1 而不是 T-1 是必须的：T-H..T-1 的前向收益在 T 日还没结算，
    用了就是未来信息。首轮四个动量设计就是在这一关被否掉的。
    """
    lag = int(horizon) + 1
    if not by_weight or PROD_DRY_WEIGHT not in by_weight:
        return WalkForwardStat(top_n, 0, None, None, None, None, {})
    chosen: list[float] = []
    fixed: list[float] = []
    picks: list[float] = []
    for i in range(warmup, len(dates)):
        end = i - lag
        if end <= MIN_GROUP:
            continue
        best_w, best_v = PROD_DRY_WEIGHT, -math.inf
        for weight, series in by_weight.items():
            hist = series[:end]
            if not hist:
                continue
            value = sum(hist) / len(hist)
            if value > best_v:
                best_w, best_v = weight, value
        chosen.append(float(by_weight[best_w][i]))
        fixed.append(float(by_weight[PROD_DRY_WEIGHT][i]))
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


def matched_spread(rows: list[dict[str, float]]) -> dict[str, Any]:
    """固定动量档后的干-湿差。

    这个数接近零**不**说明 dry_q 无用——它起作用的方式是改变在动量维度上的
    落点，不是在同一动量档内挑票。报告里保留它是为了防止下一轮有人只测这个
    就下「无用」的结论。
    """
    usable = [r for r in rows if r.get("spread") is not None]
    if len(usable) < MIN_DAYS:
        return {"days": len(usable), "note": "样本不足"}
    spreads = [float(r["spread"]) for r in usable]
    by_q: dict[int, list[float]] = {}
    for row, value in zip(usable, spreads, strict=True):
        by_q.setdefault(quarter_of(int(row["date"])), []).append(value)
    t = tstat(spreads)
    return {
        "days": len(usable),
        "spread": _round(mean(spreads)),
        "spread_t": _round(t, 2),
        "by_quarter": {str(q): _round(mean(v), 3) for q, v in sorted(by_q.items())},
        "note": "接近零不代表 dry_q 无用：它改变的是动量维度上的落点，不是档内排序",
    }


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


def _band_table(bands: list[BandStat]) -> list[str]:
    lines = [
        "### dry_q 无条件分档（域内超额）",
        "",
        _row(["档位", "天数", "均只数", "档内", "t", "超额", "t", "判定"]),
        _row(["---"] * 8),
    ]
    for band in bands:
        lines.append(
            _row(
                [
                    band.label,
                    str(band.days),
                    _plain(band.avg_size, 1),
                    _signed(band.inside),
                    _plain(band.inside_t),
                    _signed(band.excess),
                    _plain(band.excess_t),
                    band.verdict,
                ]
            )
        )
    return lines


def _ablation_table(stats: list[AblationStat]) -> list[str]:
    lines = [
        "### 留 vs 去 dry_q（逐日配对）",
        "",
        _row(["topN", "天数", "留", "t", "去", "t", "配对差", "t", "正季度", "重合度", "随机带", "判定"]),
        _row(["---"] * 12),
    ]
    for stat in stats:
        band = "—"
        if stat.rand_min is not None and stat.rand_max is not None:
            band = f"{_signed(stat.rand_min)}~{_signed(stat.rand_max)}"
        lines.append(
            _row(
                [
                    f"top{stat.top_n}",
                    str(stat.days),
                    _signed(stat.keep),
                    _plain(stat.keep_t),
                    _signed(stat.drop),
                    _plain(stat.drop_t),
                    f"**{_signed(stat.diff)}**",
                    f"**{_plain(stat.diff_t)}**",
                    stat.positive_quarters,
                    _pct(stat.overlap),
                    band,
                    stat.verdict,
                ]
            )
        )
    return lines


def _weight_table(weights: dict[int, list[WeightStat]]) -> list[str]:
    lines = ["### 权重网格（域内超额）", ""]
    for top_n, stats in sorted(weights.items()):
        lines += [
            f"**top{top_n}**",
            "",
            _row(["权重", "天数", "臂内", "t", "超额", "t", ""]),
            _row(["---"] * 7),
        ]
        for stat in stats:
            lines.append(
                _row(
                    [
                        _plain(stat.weight, 2),
                        str(stat.days),
                        _signed(stat.inside),
                        _plain(stat.inside_t),
                        _signed(stat.excess),
                        _plain(stat.excess_t),
                        "← 生产" if stat.is_production else "",
                    ]
                )
            )
        lines.append("")
    return lines


def _walk_forward_table(stats: list[WalkForwardStat]) -> list[str]:
    lines = [
        "### 走前动态选权重 vs 固定 0.20",
        "",
        _row(["topN", "天数", "动态", "固定", "差值", "t", "选中分布", "判定"]),
        _row(["---"] * 8),
    ]
    for stat in stats:
        dist = "—"
        if stat.pick_dist:
            top = sorted(stat.pick_dist.items(), key=lambda kv: -kv[1])[:3]
            dist = " / ".join(f"{k:.2f}:{v * 100:.0f}%" for k, v in top)
        lines.append(
            _row(
                [
                    f"top{stat.top_n}",
                    str(stat.days),
                    _signed(stat.chosen),
                    _signed(stat.fixed),
                    _signed(stat.diff),
                    _plain(stat.diff_t),
                    dist,
                    stat.verdict,
                ]
            )
        )
    return lines


def _dry_action(stats: list[AblationStat]) -> str:
    ready = [s for s in stats if s.days >= MIN_DAYS and s.diff_t is not None]
    if not ready:
        return "① dry_q：样本不足，本轮无结论，先补数据。"
    support = [s for s in ready if s.verdict.startswith("显著且抵成本")]
    if len(support) == len(ready):
        return (
            f"① dry_q 保留：{len(ready)}/{len(ready)} 格均显著（t>2）、高于成本 "
            f"{ROUND_TRIP_COST_PCT}%、且在随机带外。**不需要改动**。"
        )
    if not support:
        return (
            "① dry_q 本轮**失去支持**：没有一格同时满足 t>2 与抵成本。"
            "去掉它（权重置 0）之前，先复核域口径和成本假设，再跨一个季度复测。"
        )
    return (
        f"① dry_q 部分支持：{len(support)}/{len(ready)} 格通过。"
        "不足以改权重，但也不构成撤除依据；下一轮看未通过的格是否稳定。"
    )


def _weight_action(walk_forward: list[WalkForwardStat], weights: dict[int, list[WeightStat]]) -> str:
    ready = [s for s in walk_forward if s.days >= MIN_DAYS and s.diff_t is not None]
    if not ready:
        return "② 权重档位：走前样本不足，维持 0.20。"
    flat = [s for s in ready if s.is_concentrated is False]
    passing = [s for s in ready if s.diff_t is not None and s.diff_t >= 2.0]
    if not passing:
        note = "（选中分布散开，是平坦目标被拟合到噪声的特征）" if flat else ""
        return f"② 权重维持 {PROD_DRY_WEIGHT}：走前动态选权重没有一格 t>2{note}，不可上线。"
    if flat:
        return (
            f"② 权重维持 {PROD_DRY_WEIGHT}：虽有 {len(passing)} 格 t>2，但选中分布散开，"
            "先确认网格上界不是边界假象，再谈调档。"
        )
    grid_hint = ""
    top_key = min(weights) if weights else None
    if top_key is not None:
        graded = [w for w in weights[top_key] if w.excess is not None]
        if graded:
            best = max(graded, key=lambda w: w.excess or 0.0)
            grid_hint = f"（top{top_key} 网格最优在 {best.weight:.2f}）"
    return (
        f"② 权重可议：{len(passing)} 格走前 t>2 且选中集中{grid_hint}。先把候选档位在下一个季度独立复测，再动生产值。"
    )


def render(report: RankerReport, *, horizon: int, start: int, end: int) -> str:
    """渲染 markdown。落到 docs/evidence，不要落 artifacts/（已被 gitignore）。"""
    lines = [
        "# 排序权重体检：dry_q",
        "",
        f"- 区间：{start} ~ {end}，前向 H={horizon} 日（T+1 开盘买、T+1+H 收盘卖）",
        # tushare amount 单位为千元，除 10 得万元
        f"- 域：20 日均额 >= {MIN_AMOUNT_RAW / 10:.0f} 万元；成本 {ROUND_TRIP_COST_PCT}%/次往返",
        f"- 生产权重：dry_q={PROD_DRY_WEIGHT}，q20={PROD_Q20_WEIGHT}，q5={PROD_Q5_WEIGHT}，q3={PROD_Q3_WEIGHT}",
        "- 打分臂含 extension_penalty，与生产同式",
        "",
    ]
    lines += _band_table(report.bands) + [""]
    lines += _ablation_table(report.ablation) + [""]
    lines += _weight_table(report.weights)
    lines += _walk_forward_table(report.walk_forward) + [""]
    spread = report.matched_spread
    if spread:
        lines += [
            "### 固定动量档后的干-湿差",
            "",
            f"- 天数 {spread.get('days')}，差值 {_signed(spread.get('spread'))}，t {_plain(spread.get('spread_t'))}",
            f"- {spread.get('note', '')}",
            "",
        ]
    lines += [
        "## 读法",
        "",
        "- `超额` 是相对**同日流动性域内全体**的超额，不是相对基准臂的增量。"
        "两个含噪臂相减等于把噪声算两遍，会让随机带宽到吞掉真信号。",
        "- 消融的 `配对差` 为「留 dry_q 减去 dry_q」的逐日配对值。同日同域同动量分、"
        "只差这一项，配对消掉了市场共同成分，是这批比法里信噪比最高的。",
        "- `随机带` 是同权重随机分位臂的取值范围。给动量分加**任何**一个搅动项都会把选择"
        "从动量极端端拉开、本身就值几毛钱，所以「比纯动量好」不构成证据，必须落在带外。",
        "- `重合度` 低（10% 量级）说明 dry_q 在做主选择而非末端微调。",
        "- `选中分布` 散开是危险信号：目标函数平坦时走前会在多个权重间乱跳，"
        "此时的显著性是拟合噪声。窄网格「总选边界值」同样是假象。",
        "- 分档呈驼峰（最干档非最优）说明收益来自躲开放量，不是越缩越好。",
        "",
        "## 接下来做什么",
        "",
        _dry_action(report.ablation),
        _weight_action(report.walk_forward, report.weights),
        (
            f"③ 任一结论要落到参数改动，需先确认增益大于单次往返成本 {ROUND_TRIP_COST_PCT}%，"
            "且跨越多个行情段后方向稳定。"
        ),
        "",
    ]
    return "\n".join(lines)
