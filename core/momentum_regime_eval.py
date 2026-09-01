"""动量体检：RPS 闸门还值不值得留，以及能不能跟着市场动态调。

2026-08-31 首轮跑了 402 个交易日（2025-01-02..2026-08-28）全市场，结论是
**动量没坏，是平的**——RPS 闸门相对流动性域内的超额 H=5 +0.05pct（t=+0.31）、
H=10 +0.07pct（t=+0.28）。之前短窗看到的「失效」是一个季度的事：

    2025Q3 +1.66   2025Q4 +1.76   2026Q1 -0.37   2026Q2 +2.32   2026Q3 -6.63   （H=10）

围绕零均值按季度摆动 ±2~7pct。三个具体发现——

**一、闸门几乎不产生选择价值。** H=10 闸门绝对 +0.432%（t=+0.96），而流动性域内基准
+0.362%（t=+1.27）。均值只高 0.07pct，t 值反而更低。它把 ret20 截面分位从 50 抬到 75，
抬完没换来可测的超额。

**二、阈值是杠杆旋钮，不是选择性旋钮。** 收紧到 75/80 让正常季度更好（+1.60）、2026Q3
更差（-7.54）；放松到 30/40 两头都收窄（+0.88 / -3.41）；全期 t 值始终在 +0.92~+1.11
之间不动。调阈值只改振幅，不改期望。

**三、四种「动态迭代」设计走前全部失效。** 样本内好看、走前失效是这一族的通病：

    按已收敛 IC 二值开关      H=5 差值 +0.176(t=+1.50)、H=10 +0.040(t=+0.27)
    按已收敛 IC 连续配比      H=5 +0.033(t=+0.24)、H=10 -0.098(t=-0.61) 反向
    按市场宽度/波动切换       最好变体 t=+1.36/+1.46，且坏季度开启率不低于好季度
    惩罚极端动量 ret20>60%    全期 t=-3.34 看着最硬，走前该带超额却是正的

IC 自身也不可预测：非重叠窗口 lag-1 rho 仅 +0.12/+0.21（重叠窗口下的 +0.84 是 H=5
前向窗口共用 4/5 造成的假象），已收敛 IC 对当日真实 IC 的 rho 是 -0.06/-0.16。

所以首轮**没有改任何参数**。这个体检保留下来，是为了持续确认「闸门期望为零、
振幅按季度摆动」这个结论，以及在它真的转为持续为负时能被看见。

第二轮（2026-09-01，1079 个交易日 2022..2026 回测快照，H=10）把样本从 402 天拉到
1079 天后，结论从「平的」变成「方向反了」，又有两条发现——

**四、beta 中性化后动量还在，所以「跟着市场动态迭代」不是对冲 beta 的事。**
ret60 中性化后保留 82% 的 Rank IC（IR -0.41 → -0.39，三段符号一致），
dry_vol_q250 保留 94%。顺带露出两个被 beta 盖住的因子：amplitude60 IR
-0.20 → **-0.72**（t=-22.0）、price_from_low250 -0.35 → **-0.59**（t=-18.2）。
但它们的单调性只有 -0.141/-0.054，和 ret60 一样不可线性化，仍只能做负筛。

**五、动量唯一可用的内容是顶部负筛，不是「越低越好」，更不是「退到中动量档」。**
按 10 分位逐带扫描，RPS50 与 RPS120 两条腿各自独立地给出同一梯度：0~60 带为正、
70~100 带为负、交叉点落在 60~70，90~100 带 -0.860（0/5 年为正）。生产的 65/70
正好压在交叉点上并向负区延伸。两个方向的阈值微调都被否掉——收紧 75/80 是 -0.687
（t=-6.27），放松 55/60 是 -0.389（t=-5.46）；闸门相对全域的超额在 5/5 个年份为负
或为零，所以这不是水温故事。**但「退到中动量档」同样不成立**：随机负控制里，只要
每天从「两条腿都 < NON_TOP_CAP 分位」随机抽同样只数，5 个种子给出 +0.164~+0.210，
与中动量档的 +0.197 无法区分；而从全域随机抽是 ~0.00。中动量档的全部边缘来自避开
顶部，不含任何选择信息——这就是本模块把随机负控制固化进每次体检的原因。

据此，当前有三处机制押在梯度的负端，第二轮**同样没有改动它们**：闸门本身
（``FunnelConfig`` 65/70）、弱市收紧（``tools/market_regime.py`` 抬到 80/75，
在它针对的弱市子样本里实测 -0.200、t=-4.80、0/5 年为正）、以及
``_boost_bias_for_strong_rps``（rps_slow>=90 放宽 bias 上限，正是 -0.860 那一带）。
按下面「走前测试的必要性」一节，动手前必须先过 ``walk_forward_switch``。

口径约定
--------
- 前向收益：T+1 开盘买、T+1+H 收盘卖，扣单次往返成本 0.202%
- 域：20 日均额 >= 8000 万元（tushare ``amount`` 单位为千元，故阈值取 80000）
- 每日等权后再跨日平均，避免入选只数多的日子主导均值
- ``excess`` 均为相对同日流动性域内全体的超额
- t 值为手工计算（环境无 scipy）

走前测试的必要性
----------------
首轮四种设计里有两种在全样本回看下 t 值超过 2，走前测试才把它们否掉。任何
新的阈值或权重改动都必须按 ``walk_forward_switch`` 的同一口径验证——那个测试
是唯一能区分「真信号」和「事后挑期」的环节。

**但走前本身也不够——``diff_t`` 单独看不是有效检验。**基线是固定放行闸门，而中
动量档全期高出闸门 0.671pct，于是任何关闭约 51% 天数的开关表都会机械地换来
~0.345pct。实测同关闭率的**随机**开关表给 +0.313~+0.419、t=+3.70~+5.17：抛硬币
就能过 t>=2 的线。所以 ``SwitchStat`` 把差值拆成机械项与择时项，判定改看条件价差
``spread_t``（关闭日与开启日的 mid-gate 之差），并且：

1. ``spread_t`` 只能用**不重叠**日算。相邻交易日的 H 日前向收益共用 H-1 天，全样本
   口径把按市场宽度切换算成 t=+3.57，而不重叠口径只有 +1.38。
2. 单个抽样相位的 t 自己就是噪声，故取遍 H+1 个相位报中位数与区间。该设计的 11 个
   相位落在 +0.08~+1.92，**0/11 个到 2**；按截面离散度切换是 -2.43~-0.86。

两种动态切换设计都不含可用的水温信息，第二轮**没有**上线任何一种。这与配对随机
控制给出的 +1.61~+2.85 是同一结论，三条独立路径互相印证。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# 生产值，用于在报告里标注「当前档位」。
PROD_RPS_FAST_MIN = 65.0
PROD_RPS_SLOW_MIN = 70.0
PROD_RPS_WINDOW_FAST = 50
PROD_RPS_WINDOW_SLOW = 120

ROUND_TRIP_COST_PCT = 0.202
MIN_AMOUNT_RAW = 80000.0
MIN_DOMAIN_SIZE = 500
MIN_GROUP = 20
MIN_DAYS = 20

# 阈值扫描：(fast_min, slow_min)。含生产值与两侧各档。
THRESHOLD_GRID: tuple[tuple[float, float], ...] = (
    (75.0, 80.0),
    (70.0, 75.0),
    (65.0, 70.0),
    (55.0, 60.0),
    (50.0, 50.0),
    (40.0, 40.0),
    (30.0, 40.0),
)

# 中动量档，用于回答「退到中间档是不是更好」。走前测试的对照必须是漏斗真能
# 输出的另一档，不能是空仓——漏斗每天都要出票。
MID_BAND = (40.0, 65.0, 40.0, 70.0)

# 顶部上限：随机负控制只从「两条腿都低于此分位」的票里抽。
# 取 80 是因为逐带扫描里 70~80 已转负、80~90 为 0/5 年正（详见模块 docstring 第五条）。
NON_TOP_CAP = 80.0
# 随机负控制的种子。多种子是为了看边缘是否稳定，单种子的一次抽样不足以判定。
CONTROL_SEEDS: tuple[int, ...] = (11, 29, 47, 83, 101)


def tstat(values: list[float]) -> float | None:
    """手工 t 值。环境无 scipy，且这里只需要单样本均值检验。"""
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(clean) < 3:
        return None
    avg = sum(clean) / len(clean)
    var = sum((v - avg) ** 2 for v in clean) / (len(clean) - 1)
    if var <= 0:
        return None
    return avg / math.sqrt(var / len(clean))


def quarter_of(trade_date: int) -> int:
    """20260815 -> 20263。用整数便于排序和 JSON 落盘。"""
    year, month = trade_date // 10000, trade_date // 100 % 100
    return year * 10 + (month - 1) // 3 + 1


@dataclass
class BandStat:
    """一档动量带的逐日汇总。"""

    label: str
    days: int
    avg_size: float
    inside_ret: float | None
    domain_ret: float | None
    excess: float | None
    excess_t: float | None
    # 绝对收益的 t 值。判断闸门是否值得留必须看它——均值高但 t 值更低意味着
    # 多承担了波动却没换来更可靠的收益。
    inside_t: float | None = None
    by_quarter: dict[int, float] = field(default_factory=dict)
    is_production: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "avg_size": round(self.avg_size, 1),
            "inside_ret": _round(self.inside_ret),
            "inside_t": _round(self.inside_t, 2),
            "domain_ret": _round(self.domain_ret),
            "excess": _round(self.excess),
            "excess_t": _round(self.excess_t, 2),
            "by_quarter": {str(k): _round(v) for k, v in sorted(self.by_quarter.items())},
            "is_production": self.is_production,
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.excess is None or self.excess_t is None:
            return "样本不足"
        if abs(self.excess_t) < 2.0:
            return "期望为零：无选择价值"
        return "正贡献" if self.excess > 0 else "负贡献"


@dataclass
class SwitchStat:
    """一种动态切换设计的走前结果，含机械项/择时项分解。

    ``diff_t`` 单独看**不是有效检验**。基线是固定放行闸门，而中动量档全期高出闸门
    0.671pct，于是任何关闭率约 51% 的开关表都会机械地换来 ~0.345pct；实测同关闭率
    的随机开关表给 +0.313~+0.419（t=+3.70~+5.17）。抛硬币就能过 t>=2 的线。

    所以要判断设计是否真在识别水温，得看 ``spread_t``：它关闭的那些天，中动量档相对
    闸门的优势是不是真的更大（两样本 Welch t，不含随机成分）。
    """

    label: str
    days: int
    switched_ret: float | None
    baseline_ret: float | None
    diff: float | None
    diff_t: float | None
    on_rate: float | None
    on_rate_by_quarter: dict[int, float] = field(default_factory=dict)
    # 机械项：关闭率 × 全期 (mid - gate)。与开关表是否含信息无关，任何同关闭率的
    # 表都拿得到。择时项 = diff - 机械项。
    mechanical: float | None = None
    # 条件价差：关闭日与开启日的 (mid - gate) 均值（用全部日子），及其两样本 Welch t。
    spread_off: float | None = None
    spread_on: float | None = None
    # t 只用不重叠日算（天数远少于 days），且取遍 H+1 个相位后的**中位数**——
    # 单个相位自己就是噪声：实测 breadth 的 11 个相位给 +0.08~+1.92。
    spread_t: float | None = None
    spread_t_min: float | None = None
    spread_t_max: float | None = None
    spread_days: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "switched_ret": _round(self.switched_ret),
            "baseline_ret": _round(self.baseline_ret),
            "diff": _round(self.diff),
            "diff_t": _round(self.diff_t, 2),
            "on_rate": _round(self.on_rate, 3),
            "on_rate_by_quarter": {str(k): _round(v, 3) for k, v in sorted(self.on_rate_by_quarter.items())},
            "mechanical": _round(self.mechanical),
            "timing": _round(self.timing),
            "spread_off": _round(self.spread_off),
            "spread_on": _round(self.spread_on),
            "spread_t": _round(self.spread_t, 2),
            "spread_t_min": _round(self.spread_t_min, 2),
            "spread_t_max": _round(self.spread_t_max, 2),
            "spread_days": self.spread_days,
            "verdict": self.verdict,
        }

    @property
    def timing(self) -> float | None:
        """择时项：diff 里扣掉任何同关闭率开关表都能拿到的机械部分。"""
        if self.diff is None or self.mechanical is None:
            return None
        return self.diff - self.mechanical

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t < 2.0:
            return "走前不显著：不可上线"
        if self.spread_t is None:
            return "机械项未分解：不可判定"
        if self.spread_t < 2.0:
            return "机械项主导：换档收益而非水温信息"
        # 中位数过线还不够：相位间波动大意味着结论取决于抽哪一相,那不是稳定的证据。
        if self.spread_t_min is not None and self.spread_t_min < 2.0:
            return "价差t 依赖抽样相位：证据不稳"
        return "走前显著：值得进一步验证"


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class MomentumReport:
    thresholds: list[BandStat] = field(default_factory=list)
    mid_band: BandStat | None = None
    domain: BandStat | None = None
    # 随机负控制，每个种子一档。中动量档要证明自己不只是「避开了顶部」，
    # 就必须比这几档更好；第二轮的结论正是它比不过。
    controls: list[BandStat] = field(default_factory=list)
    switches: list[SwitchStat] = field(default_factory=list)
    ic_persistence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholds": [s.as_dict() for s in self.thresholds],
            "mid_band": None if self.mid_band is None else self.mid_band.as_dict(),
            "domain": None if self.domain is None else self.domain.as_dict(),
            "controls": [s.as_dict() for s in self.controls],
            "control_gap": control_gap(self.mid_band, self.controls),
            "switches": [s.as_dict() for s in self.switches],
            "ic_persistence": self.ic_persistence,
            "production": {
                "rps_fast_min": PROD_RPS_FAST_MIN,
                "rps_slow_min": PROD_RPS_SLOW_MIN,
                "rps_window_fast": PROD_RPS_WINDOW_FAST,
                "rps_window_slow": PROD_RPS_WINDOW_SLOW,
            },
            "reading": (
                "excess 为相对同日流动性域内全体的超额。闸门要证明自己有价值，"
                "需要 excess_t 显著为正**且**绝对收益的 t 值高于域内基准——"
                "首轮两条都不成立。controls 是每天从「两条腿都低于 "
                f"{NON_TOP_CAP:.0f} 分位」随机抽同样只数的负控制：中动量档只有明显"
                "跑赢它才算含选择信息，否则它的边缘只是「避开了顶部」。"
                "switches 的 diff_t 是走前口径，但它单独看不是有效检验——机械项那部分"
                "任何同关闭率的随机开关表都拿得到，判断择时能力要看 spread_t。"
            ),
        }


def summarize_band(
    label: str,
    daily: list[dict[str, float]],
    *,
    is_production: bool = False,
) -> BandStat:
    """把逐日观测汇总成一档统计。每日等权，避免入选只数多的日子主导均值。"""
    usable = [r for r in daily if r.get("inside") is not None and r.get("domain") is not None]
    if len(usable) < MIN_DAYS:
        return BandStat(label, len(usable), 0.0, None, None, None, None, None, {}, is_production)
    insides = [float(r["inside"]) for r in usable]
    diffs = [float(r["inside"]) - float(r["domain"]) for r in usable]
    by_quarter: dict[int, list[float]] = {}
    for row, diff in zip(usable, diffs, strict=True):
        by_quarter.setdefault(quarter_of(int(row["date"])), []).append(diff)
    return BandStat(
        label=label,
        days=len(usable),
        avg_size=sum(float(r.get("size") or 0) for r in usable) / len(usable),
        inside_ret=sum(insides) / len(insides),
        domain_ret=sum(float(r["domain"]) for r in usable) / len(usable),
        excess=sum(diffs) / len(diffs),
        excess_t=tstat(diffs),
        inside_t=tstat(insides),
        by_quarter={q: sum(v) / len(v) for q, v in by_quarter.items()},
        is_production=is_production,
    )


def control_gap(mid: BandStat | None, controls: list[BandStat]) -> dict[str, Any]:
    """中动量档相对随机负控制的差距。

    这是第二轮唯一能否掉「退到中动量档」的环节。控制组每天从「两条腿都低于
    ``NON_TOP_CAP`` 分位」里随机抽同样只数，因此它只带「避开顶部」这一条信息、
    不带任何选择信息。中动量档若落在控制组的区间内，说明它的边缘同样只是避开
    顶部——**这种情况下不能把它当成一个可选档位提案**。

    差距按各种子超额的均值算；``seed_spread`` 给出控制组自身的抽样边缘宽度，
    差距小于这个宽度就谈不上「跑赢」。
    """
    usable = [c.excess for c in controls if c.excess is not None]
    if mid is None or mid.excess is None or len(usable) < 2:
        return {"verdict": "样本不足", "seeds": len(usable)}
    avg = sum(usable) / len(usable)
    spread = max(usable) - min(usable)
    gap = mid.excess - avg
    inside = min(usable) <= mid.excess <= max(usable)
    return {
        "seeds": len(usable),
        "mid_excess": _round(mid.excess),
        "control_excess_avg": _round(avg),
        "control_excess_min": _round(min(usable)),
        "control_excess_max": _round(max(usable)),
        "seed_spread": _round(spread),
        "gap": _round(gap),
        "verdict": (
            "中动量档落在随机负控制区间内：边缘仅来自避开顶部，不含选择信息"
            if inside or gap <= spread
            else "中动量档跑赢随机负控制：含独立选择信息，值得按走前口径复验"
        ),
    }


def walk_forward_switch(
    label: str,
    rows: list[dict[str, float]],
    *,
    state_key: str,
    warmup: int = 120,
    high_is_on: bool = True,
    horizon: int = 1,
) -> SwitchStat:
    """走前切换：T 日只用 T 之前的历史定阈值，关闭时退到中动量档而非空仓。

    这是唯一能否掉「样本内回看显著」的环节。首轮四种设计全部在这里失效。

    ``horizon`` 是前向收益天数。传了它，``spread_t`` 会按 H+1 步长取不重叠日再算——
    相邻交易日的 H 日前向收益共用 H-1 天，直接算两样本 t 会像 ``ic_persistence``
    里那个 +0.906 的假象一样把 t 夸大。而单个相位（只取 offset=0）本身又是噪声，
    所以取遍 H+1 个相位后报中位数，并把最小/最大值一起带出来。

    1080 天 H=10 实测：按市场宽度切换的重叠口径 t=+3.57，11 个相位给 +0.08~+1.92
    （中位 +1.38），**0/11 个相位到 2**；按截面离散度切换 -2.43~-0.86。所以两种设计
    都不含可用的水温信息，与配对随机控制给出的 +1.61~+2.85 是同一结论。
    """
    usable = [r for r in rows if r.get("gate") is not None and r.get("mid") is not None]
    if len(usable) <= warmup + MIN_DAYS:
        return SwitchStat(label, 0, None, None, None, None, None, {})
    switched, baseline, on_flags, dates = [], [], [], []
    history: list[float] = [float(usable[i][state_key]) for i in range(warmup)]
    for row in usable[warmup:]:
        current = float(row[state_key])
        threshold = sorted(history)[len(history) // 2]
        history.append(current)
        is_on = current > threshold if high_is_on else current <= threshold
        switched.append(float(row["gate"] if is_on else row["mid"]))
        baseline.append(float(row["gate"]))
        on_flags.append(is_on)
        dates.append(int(row["date"]))
    diffs = [s - b for s, b in zip(switched, baseline, strict=True)]
    on_by_quarter: dict[int, list[bool]] = {}
    for date, flag in zip(dates, on_flags, strict=True):
        on_by_quarter.setdefault(quarter_of(date), []).append(flag)
    off_rate = 1.0 - sum(on_flags) / len(on_flags)
    spread = [float(r["mid"]) - float(r["gate"]) for r in usable[warmup:]]
    paired = list(zip(spread, on_flags, strict=True))
    # 均值用全部日子（无偏且更精），只有 t 值需要独立样本：按 H+1 步长抽不重叠日，
    # 与 ic_persistence 同一约定。单个相位的 t 自己就是噪声，故取遍相位再取中位数。
    step = max(int(horizon) + 1, 1)
    phase_ts = [t for off in range(step) if (t := _phase_welch_t(paired[off::step])) is not None]
    spread_off = [v for v, on in paired if not on]
    spread_on = [v for v, on in paired if on]
    return SwitchStat(
        label=label,
        days=len(switched),
        switched_ret=sum(switched) / len(switched),
        baseline_ret=sum(baseline) / len(baseline),
        diff=sum(diffs) / len(diffs),
        diff_t=tstat(diffs),
        on_rate=sum(on_flags) / len(on_flags),
        on_rate_by_quarter={q: sum(v) / len(v) for q, v in on_by_quarter.items()},
        mechanical=off_rate * (sum(spread) / len(spread)),
        spread_off=_mean_or_none(spread_off),
        spread_on=_mean_or_none(spread_on),
        spread_t=_median_or_none(phase_ts),
        spread_t_min=min(phase_ts) if phase_ts else None,
        spread_t_max=max(phase_ts) if phase_ts else None,
        spread_days=len(paired[::step]),
    )


def _phase_welch_t(rows: list[tuple[float, bool]]) -> float | None:
    """一个抽样相位内的两样本 Welch t。"""
    return welch_t([v for v, on in rows if not on], [v for v, on in rows if on])


def _median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _mean_or_none(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def welch_t(a: list[float], b: list[float]) -> float | None:
    """两样本 Welch t。关闭日与开启日天数不等、方差也不必相等，故不能用合并方差。"""
    if len(a) < 3 or len(b) < 3:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se > 0 else None


def ic_persistence(daily_ic: list[float], horizon: int) -> dict[str, Any]:
    """动量 IC 的自持续性，**必须用非重叠窗口**。

    相邻的 H=5 日度 IC 共用 4/5 的前向窗口，会凭空造出 lag-1 rho ≈ +0.84。
    首轮差点把它读成「动量有持续性」。这里按 H+1 步长取不重叠样本。
    """
    clean = [float(v) for v in daily_ic if v is not None and math.isfinite(float(v))]
    step = max(int(horizon) + 1, 1)
    closed = clean[::step]
    naive = _lag1_corr(clean)
    honest = _lag1_corr(closed)
    signs = [1 if v > 0 else 0 for v in closed]
    persist = (
        sum(1 for a, b in zip(signs, signs[1:], strict=False) if a == b) / (len(signs) - 1) if len(signs) > 1 else None
    )
    return {
        "segments": len(closed),
        "lag1_overlapping": _round(naive, 3),
        "lag1_non_overlapping": _round(honest, 3),
        "sign_persistence": _round(persist, 3),
        "note": "overlapping 值仅作对照，它被窗口重叠夸大，不可用于判断持续性。",
    }


def _lag1_corr(series: list[float]) -> float | None:
    if len(series) < 4:
        return None
    left, right = series[:-1], series[1:]
    n = len(left)
    mean_l, mean_r = sum(left) / n, sum(right) / n
    cov = sum((a - mean_l) * (b - mean_r) for a, b in zip(left, right, strict=True))
    var_l = sum((a - mean_l) ** 2 for a in left)
    var_r = sum((b - mean_r) ** 2 for b in right)
    if var_l <= 0 or var_r <= 0:
        return None
    return cov / math.sqrt(var_l * var_r)


def render(report: MomentumReport, horizon: int) -> str:
    quarters = sorted({q for stat in report.thresholds for q in stat.by_quarter})
    head = "| RPS 闸门 | 天数 | 日均入选 | 绝对 | 绝对t | 超额 | 超额t | " + " | ".join(str(q) for q in quarters) + " |"
    lines = [
        f"**动量体检｜T+{horizon}**",
        "",
        head,
        "| --- | --: | --: | --: | --: | --: | --: |" + " --: |" * len(quarters),
    ]
    for stat in report.thresholds:
        lines.append(_band_row(stat, quarters))
    if report.mid_band is not None:
        lines.append(_band_row(report.mid_band, quarters))
    for stat in report.controls:
        lines.append(_band_row(stat, quarters))
    if report.domain is not None:
        lines.append(_band_row(report.domain, quarters))
    lines += [
        "",
        "| 动态切换设计 | 天数 | 切换后 | 固定放行 | 差值 | 差值t | 机械项 | 择时项 | 价差t | 开启率 | 判定 |",
        "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for stat in report.switches:
        lines.append(
            f"| {stat.label} | {stat.days} | {_signed(stat.switched_ret)} | {_signed(stat.baseline_ret)} | "
            f"{_num(stat.diff)} | {_num(stat.diff_t, 2)} | {_num(stat.mechanical)} | {_num(stat.timing)} | "
            f"{_spread_t_cell(stat)} | {_pct(stat.on_rate)} | {stat.verdict} |"
        )
    lines.append(
        "　注：差值t 单独看不是有效检验——基线是固定放行闸门，中动量档全期高出闸门，"
        "任何同关闭率的**随机**开关表都会机械地拿到「机械项」那部分（实测 t=+3.7~+5.2）。"
        "判断设计是否真在识别水温看 **价差t**：它关闭的那些天，中动量档的优势是否真的更大。"
        "价差t 报的是 **H+1 个抽样相位的中位数**，方括号是相位间区间、括号是每相位的"
        "**不重叠**天数——相邻日的 H 日前向收益共用 H-1 天，按全部日子算会把 t 夸大"
        "一倍以上（实测 +3.57 vs 相位区间 +0.08~+1.92）；均值仍用全部日子。"
        "区间下界不到 2 就判「证据不稳」：结论取决于抽哪一相位，不算站得住。"
    )
    lines += ["", _ic_line(report.ic_persistence), "", "**接下来做什么**", *_actions(report)]
    return "\n".join(lines)


def _band_row(stat: BandStat, quarters: list[int]) -> str:
    name = f"**{stat.label}（生产值）**" if stat.is_production else stat.label
    cells = " | ".join(_num(stat.by_quarter.get(q)) for q in quarters)
    size = "—" if stat.avg_size <= 0 else f"{stat.avg_size:.0f}"
    return (
        f"| {name} | {stat.days} | {size} | {_signed(stat.inside_ret)} | {_num(stat.inside_t, 2)} | "
        f"{_num(stat.excess)} | {_num(stat.excess_t, 2)} | {cells} |"
    )


def _spread_t_cell(stat: SwitchStat) -> str:
    """价差 t 的中位数 + 相位区间 + 不重叠天数。

    三样都得写出来：不写天数,读者会以为它和 days 一样多；不写区间,读者看不出
    结论有多依赖抽哪一相位。
    """
    if stat.spread_t is None:
        return "—"
    cell = f"{stat.spread_t:+.2f}"
    if stat.spread_t_min is not None and stat.spread_t_max is not None:
        cell += f" [{stat.spread_t_min:+.2f}, {stat.spread_t_max:+.2f}]"
    if stat.spread_days is not None:
        cell += f"（{stat.spread_days}天）"
    return cell


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.3f}%"


def _num(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _ic_line(payload: dict[str, Any]) -> str:
    if not payload:
        return "**IC 持续性**　样本不足。"
    return (
        f"**IC 持续性**　非重叠窗口 lag-1 rho={_num(payload.get('lag1_non_overlapping'), 3)}"
        f"（重叠口径 {_num(payload.get('lag1_overlapping'), 3)} 是假象），"
        f"符号持续率 {_pct(payload.get('sign_persistence'))}，{payload.get('segments')} 个不重叠段。"
        "rho 接近零意味着「按动量自身近期表现调仓」没有可利用的信息。"
    )


def _actions(report: MomentumReport) -> list[str]:
    out = []
    prod = next((s for s in report.thresholds if s.is_production), None)
    if prod is None or prod.excess_t is None:
        out.append("- ① 生产档样本不足，继续积累。")
    elif abs(prod.excess_t) < 2.0:
        out.append(
            f"- ① 生产档超额 {prod.excess:+.3f}pct（t={prod.excess_t:+.2f}）不显著，"
            "闸门期望为零。**不要为了改善均值去调阈值**——扫描显示阈值只改振幅。"
        )
    else:
        direction = "正" if prod.excess > 0 else "负"
        out.append(f"- ① 生产档超额显著为{direction}（t={prod.excess_t:+.2f}），与首轮「期望为零」不同，需复核。")
    out.append(_domain_action(report))
    out.append(_control_action(report))
    out.append(_switch_action(report))
    out.append("- ⑤ 任何参数改动落地前，须按本脚本 walk_forward_switch 的走前口径复验，且增益需大于成本 0.202%。")
    return out


def _switch_action(report: MomentumReport) -> str:
    """切换设计的读法。差值显著但价差不显著,说明赚的是换档而非择时。"""
    passed = [s for s in report.switches if s.verdict == "走前显著：值得进一步验证"]
    if passed:
        names = "、".join(
            f"{s.label}（择时项 {s.timing:+.3f}pct、价差 t={s.spread_t:+.2f} 于 {s.spread_days or '—'} 个不重叠日）"
            for s in passed
            if s.timing is not None and s.spread_t is not None
        )
        return f"- ④ 这些切换设计的择时项在扣掉机械项后仍显著：{names or '见上表'}——值得进一步验证再考虑上线。"
    mechanical = [s for s in report.switches if s.verdict == "机械项主导：换档收益而非水温信息"]
    if mechanical:
        names = "、".join(s.label for s in mechanical)
        return (
            f"- ④ {names} 的差值显著但**价差 t 不足 2**，赚的是「中动量档比闸门好」这个换档收益、"
            "不是水温信息——同关闭率的随机开关表拿得到同样的钱。这不构成上线理由。"
        )
    unstable = [s for s in report.switches if s.verdict == "价差t 依赖抽样相位：证据不稳"]
    if unstable:
        names = "、".join(
            f"{s.label}（中位 {s.spread_t:+.2f}，相位区间 [{s.spread_t_min:+.2f}, {s.spread_t_max:+.2f}]）"
            for s in unstable
            if s.spread_t is not None and s.spread_t_min is not None and s.spread_t_max is not None
        )
        return (
            f"- ④ {names or '见上表'} 的价差 t 中位数过线但**相位区间下界不到 2**——换个不重叠"
            "抽样相位结论就翻，这不算站得住的证据。要么把样本拉长到相位区间整体过线，要么放弃。"
        )
    undecided = [s for s in report.switches if s.verdict == "机械项未分解：不可判定"]
    if undecided:
        names = "、".join(s.label for s in undecided)
        return (
            f"- ④ {names} 的差值显著，但开关表几乎全程只落在一侧、没有对照日，价差无法检验。"
            "这种情形下差值全是机械项，**不能读成走前通过**——先确认水温字段本身是否退化成了常量。"
        )
    return "- ④ 所有动态切换设计走前均不显著，维持固定放行。样本内回看显著不算数。"


def _control_action(report: MomentumReport) -> str:
    """随机负控制的读法。中动量档跑不赢它，就不能当成一个可选档位提案。"""
    gap = control_gap(report.mid_band, report.controls)
    if gap.get("gap") is None:
        return "- ③ 随机负控制样本不足，无法判断中动量档是否含选择信息。"
    if "落在" in str(gap.get("verdict")):
        return (
            f"- ③ 中动量档超额 {gap['mid_excess']:+.3f}pct 落在随机负控制区间 "
            f"[{gap['control_excess_min']:+.3f}, {gap['control_excess_max']:+.3f}] 内（{gap['seeds']} 个种子），"
            "说明它的边缘只来自避开顶部、不含选择信息。**动量可用的内容是「顶部要躲开」，"
            "不是「越低越好」，也不是「退到中动量档」**——不要把中动量档当成档位提案。"
        )
    return (
        f"- ③ 中动量档超额比随机负控制均值高 {gap['gap']:+.3f}pct，超过控制组自身的抽样宽度 "
        f"{gap['seed_spread']:+.3f}pct（{gap['seeds']} 个种子），与第二轮结论不同，"
        "需按 walk_forward_switch 的走前口径复核后再谈上线。"
    )


def _domain_action(report: MomentumReport) -> str:
    """对照域内基准。均值高不算赢——要连绝对收益的 t 值一起比才算。

    首轮闸门均值高 0.07pct 而 t 值反而更低（+0.28 vs +1.27），即多承担了波动却
    没换来更可靠的收益。只看均值会把这种情况误读成「支持保留」。
    """
    prod = next((s for s in report.thresholds if s.is_production), None)
    dom = report.domain
    if prod is None or dom is None or prod.inside_ret is None or dom.inside_ret is None:
        return "- ② 域内基准对照样本不足。"
    gain = prod.inside_ret - dom.inside_ret
    prod_t, dom_t = prod.inside_t, dom.inside_t
    if prod_t is not None and dom_t is not None and prod_t <= dom_t:
        return (
            f"- ② 闸门绝对收益 {prod.inside_ret:+.3f}%（t={prod_t:+.2f}）对域内基准 "
            f"{dom.inside_ret:+.3f}%（t={dom_t:+.2f}）只多 {gain:+.3f}pct 而 **t 值更低**——"
            "多承担了波动没换来更可靠的收益，闸门的存在需要独立理由（如控制持仓集中度）。"
        )
    if gain <= ROUND_TRIP_COST_PCT:
        return (
            f"- ② 闸门绝对收益比域内基准只高 {gain:+.3f}pct，小于单次往返成本 "
            f"{ROUND_TRIP_COST_PCT}%，净收益上看不出差别。"
        )
    return (
        f"- ② 闸门绝对收益 {prod.inside_ret:+.3f}%（t={prod_t:+.2f}）高于域内基准 "
        f"{dom.inside_ret:+.3f}%（t={dom_t:+.2f}）且增益 {gain:+.3f}pct 超过成本，本轮支持保留。"
    )
