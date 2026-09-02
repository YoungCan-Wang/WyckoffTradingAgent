"""动量体检：RPS 闸门还值不值得留，以及能不能跟着市场动态调。

⚠️ 首轮那句「动量没坏，是平的」的样本区间写错了，2026-09-01 已更正：**取了 402 天
行情，真正评估的只有 271 天（2025-07-04..2026-08-13）**——RPS 慢腿 120 日预热把起点
往后推了半年，尾部又扣掉 H+1 天。402 天从未被评估过。同一份行情把取数起点提到
2024-01 得到 513 个评估日，生产档超额从 **+0.070（t=+0.28）「期望为零」翻成
-0.320（t=-2.08）「负贡献」**（H=5 是 +0.050 → -0.151）。所以下面首轮那三条结论
只在 271 天的短窗里成立，读之前先看产物里的 ``eval_window``。

首轮（271 个评估日）读数是 **动量没坏，是平的**——RPS 闸门相对流动性域内的超额
H=5 +0.05pct（t=+0.31）、H=10 +0.07pct（t=+0.28）。之前短窗看到的「失效」被判成
一个季度的事：

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

所以首轮**没有改任何参数**。这个体检保留下来，是为了持续确认闸门的方向，以及在它
真的转为持续为负时能被看见——而这恰恰要求样本足够长：271 天读「期望为零」、513 天
读「负贡献」，短窗看不见的正是它要看见的东西。

第二轮（2026-09-01，1079 个交易日 2022..2026 回测快照，H=10）把样本从 271 天拉长后，
结论从「平的」变成「方向反了」，又有两条发现——第三轮在 513 天上独立复现了这个方向
（生产档 -0.320、t=-2.08），所以它不是那份快照特有的。

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

第四轮（2026-09-02，513/518 个评估日 2024-07-04..2026-08-13，H=10/5）直接回答「动量
在现在的市场还合不合适、能不能跟市场动态迭代」这两问。答案是：**不合适，但也没法动态
调**——所以仍然不改任何阈值。

先看「合不合适」。生产闸门在 513 天上是净负贡献（超额 -0.320pct、t=-2.08），而且梯度
**不稳定到会整体反号**。按 RPS120 十分位、H=10 分半年看，顶部带与底部带同时翻面：

    顶部带  24H2 -2.08   25H1 -0.82   25H2 -0.11   26H1 **+2.64**   26H2 -5.37
    底部带  24H2 +1.15   25H1 -0.07   25H2 -0.00   26H1 **-1.12**   26H2 +3.50

26H1 整个半年「越强越好」，26H2 又翻回「越强越差」且幅度翻倍。顶部带超额自身的不重叠
lag-1 rho 只有 +0.247（月）/-0.227（双月）/+0.334（季），即**上一段的方向预测不了下一
段**。这正是任何「跟着近期梯度动态开关」都注定失效的根源。

**六、顶部负筛的方向是真的，但证不出来，而且不能跟着市场动态开关。**闸门内再剔掉
「任一腿 >= 90 分位」这一档，超额回收 H=10 +0.233pct、H=5 +0.152pct，5 个半年里 4 个
为正——方向与第二轮的逐带扫描一致。但判据是**不重叠相位**：H=10 全样本 t=+3.06，而
11 个相位全部落在 +0.59~+1.29，**0/11 个到 2**（相邻日的 H 日前向收益共用 H-1 天，全
样本口径把 t 夸大一倍以上）。所以静态负筛达不到上线标准。

动态版本更彻底：以「过去 LOOK 天已实现的顶部梯度」为状态（回退 H+1 天防穿越）做走前
切换，LOOK=40/60/120 与两个 horizon 共六格里，只有 H=10 / LOOK=60 的 ``diff_t`` 过线
（差值 +0.043、t=+2.84）。而它的**环移负控制**给 -0.186~+0.238、其中一个到 t=+3.63：
把状态与同日前向收益的对齐关系整体打乱后，还能拿到比真设计更高的 t。真值落在控制区间
内，这一格是噪声。

这里的控制必须是**环移**而不是均匀随机状态：随机序列没有自相关，走前阈值取历史中位数
会把开启率钉在 ~0.5（实测三个种子 0.480/0.486/0.497），而真设计是 0.88；两者机械项
（关闭率 × 全期价差）不同，比出来的是关闭率的差而不是信息的差——与
``uniform-band-control-is-biased`` 同一类错。环移保留取值分布与自相关，只打断待检的那条
对齐关系。**但环移不保留走前开启率**（扩张窗口中位数对漂移序列路径依赖，实测控制落在
0.366~0.793），所以判定同时看差值与**择时项**：真设计择时项 +0.038 落在控制的
-0.196~+0.224 内，三个 LOOK 档在这个开启率中性的口径下同样全部被吃掉。
见 ``shifted_switch_controls`` 与 ``switch_control_gap``。

**七、bias-200 不是独立旋钮，是动量的代理，所以 ``_boost_bias_for_strong_rps`` 没有
额外押注。**第二轮把它列为「押在梯度负端的三处机制」之一，但那个 -0.860 是在全体流动
性域上无条件测的，而这个 boost 的总体是「趋势通道标签 + ret120>=40%」、旋钮是 bias-200
上限——按 ``same-subpopulation-control``，正确的检验是在该子总体内看 bias 的梯度。实测
原始梯度强且单调（``<=35`` +0.750 t=+3.25 → ``>80`` -1.086 t=-3.35），但按同日 ret120
五分位去均值后，每一带的残差都是 |t|<2（+0.084 / -0.039 / -0.072 / -0.209）。也就是说
它放宽 bias 上限只是多放进了顶部动量票，等于已知的那条梯度，不是另一笔独立的坏赌注。
第四轮据此**不动** ``_boost_bias_for_strong_rps``。
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

# 顶部负筛的分位。第四轮检验「闸门内部再把顶部剔掉」，两条腿任一到此分位即算顶部：
# 逐带扫描里 80~90 与 90~100 都为负，而单看一条腿会漏掉另一条腿冲顶的票。
TOP_SCREEN_PCT = 90.0

# 开关表环移负控制的位移量。取素数且跨度拉开，避免与状态自身的周期共振。
SWITCH_SHIFTS: tuple[int, ...] = (37, 73, 109, 151, 199, 241)


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
    # 第四轮：闸门内部再剔掉顶部这一档，以及它跟着市场动态开关的走前结果。
    # top_screen 与生产闸门同域同日，差值即「负筛值不值得做」；screen_switches 的
    # 每一项都配一组环移负控制，见 shifted_switch_controls。
    top_screen: BandStat | None = None
    screen_switches: list[tuple[SwitchStat, list[SwitchStat]]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholds": [s.as_dict() for s in self.thresholds],
            "mid_band": None if self.mid_band is None else self.mid_band.as_dict(),
            "domain": None if self.domain is None else self.domain.as_dict(),
            "controls": [s.as_dict() for s in self.controls],
            "control_gap": control_gap(self.mid_band, self.controls),
            "switches": [s.as_dict() for s in self.switches],
            "top_screen": None if self.top_screen is None else self.top_screen.as_dict(),
            "screen_switches": [
                {
                    "design": design.as_dict(),
                    "shift_controls": [c.as_dict() for c in ctls],
                    "shift_gap": switch_control_gap(design, ctls),
                }
                for design, ctls in self.screen_switches
            ],
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


def shifted_switch_controls(
    label: str,
    rows: list[dict[str, float]],
    *,
    state_key: str,
    shifts: tuple[int, ...] = SWITCH_SHIFTS,
    warmup: int = 120,
    high_is_on: bool = True,
    horizon: int = 1,
) -> list[SwitchStat]:
    """开关表的匹配负控制：把状态序列整体**环移**后重跑同一个走前切换。

    为什么不能用均匀随机状态当控制：随机状态没有自相关，走前阈值取历史中位数，于是
    开启率恒被钉在 ~0.5（实测三个种子 0.480/0.486/0.497），而真设计的状态是强自相关
    的漂移序列、开启率 0.88。这与 ``uniform-band-control-is-biased`` 是同一类错：控制
    组必须与被检验对象在**除待检性质以外**的维度上尽量匹配。

    环移保留状态的取值多重集合与自相关结构，只打断「状态与同日前向收益的对齐」——
    待检的正是那条对齐关系。**但它不保留走前开启率**：阈值是扩张窗口的中位数，对
    漂移序列是路径依赖的，实测真设计 0.878 而 6 个环移控制落在 0.366~0.793。所以
    ``switch_control_gap`` 同时看 ``diff`` 和**择时项** ``timing``——后者已经扣掉了
    机械项（关闭率 × 全期价差），是开启率中性的那一半。

    第四轮实测（H=10、LOOK=60、顶部梯度开关，见模块 docstring 第六条）：真设计差值
    +0.043（``diff_t``=+2.84）、择时项 +0.038；6 个环移控制的差值给 -0.186~+0.238
    （其中一个到 t=+3.63）、择时项给 -0.196~+0.224。两个口径下真值都落在控制区间
    内，**打乱对齐还能拿到更高的 t**。单看 ``diff_t`` 过线会把它读成有效设计，这一层
    控制是唯一能否掉它的环节。
    """
    usable = [r for r in rows if r.get(state_key) is not None]
    if len(usable) <= warmup + MIN_DAYS:
        return []
    states = [float(r[state_key]) for r in usable]
    out: list[SwitchStat] = []
    for shift in shifts:
        step = shift % len(states)
        if step == 0:
            continue
        rotated = states[step:] + states[:step]
        control = [{**row, state_key: value} for row, value in zip(usable, rotated, strict=True)]
        out.append(
            walk_forward_switch(
                f"{label}｜环移 {shift}",
                control,
                state_key=state_key,
                warmup=warmup,
                high_is_on=high_is_on,
                horizon=horizon,
            )
        )
    return out


def switch_control_gap(design: SwitchStat, controls: list[SwitchStat]) -> dict[str, Any]:
    """真设计相对环移控制区间的位置。落在区间内即为「对齐关系不含信息」。

    判定看 ``diff`` **和择时项** ``timing``，两者任一落在控制区间内即判为不含信息。
    加 ``timing`` 是因为环移不保留走前开启率（见 ``shifted_switch_controls``），而
    ``diff`` 里含着机械项 = 关闭率 × 全期价差；``timing`` 已扣掉它，是开启率中性的
    那一半。要判「有信息」，得两个口径都出圈。

    不看 ``diff_t``——``diff_t`` 正是这一层要否掉的东西，拿它当判据就循环了。
    """
    diffs = [c.diff for c in controls if c.diff is not None]
    if design.diff is None or len(diffs) < 2:
        return {"verdict": "样本不足", "seeds": len(diffs)}
    lo, hi = min(diffs), max(diffs)
    inside = lo <= design.diff <= hi
    timings = [c.timing for c in controls if c.timing is not None]
    timing_inside = None
    if design.timing is not None and len(timings) >= 2:
        timing_inside = min(timings) <= design.timing <= max(timings)
        inside = inside or timing_inside
    return {
        "seeds": len(diffs),
        "design_diff": _round(design.diff),
        "design_diff_t": _round(design.diff_t, 2),
        "design_timing": _round(design.timing),
        "control_diff_min": _round(lo),
        "control_diff_max": _round(hi),
        "control_diff_t_max": _round(max((c.diff_t for c in controls if c.diff_t is not None), default=None), 2),
        "control_timing_min": _round(min(timings, default=None)),
        "control_timing_max": _round(max(timings, default=None)),
        "timing_inside_control": timing_inside,
        "inside_control": inside,
        "verdict": (
            "差值或择时项落在环移负控制区间内：状态与收益的对齐关系不含信息"
            if inside
            else "两个口径都超出环移负控制区间：对齐关系可能含信息，需按价差 t 复核"
        ),
    }


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


def render(report: MomentumReport, horizon: int, window: dict[str, int] | None = None) -> str:
    quarters = sorted({q for stat in report.thresholds for q in stat.by_quarter})
    head = "| RPS 闸门 | 天数 | 日均入选 | 绝对 | 绝对t | 超额 | 超额t | " + " | ".join(str(q) for q in quarters) + " |"
    lines = [
        f"**动量体检｜T+{horizon}**",
        "",
        _window_line(window),
        "",
        head,
        "| --- | --: | --: | --: | --: | --: | --: |" + " --: |" * len(quarters),
    ]
    for stat in report.thresholds:
        lines.append(_band_row(stat, quarters))
    if report.top_screen is not None:
        lines.append(_band_row(report.top_screen, quarters))
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
    lines += _screen_switch_lines(report)
    lines += ["", _ic_line(report.ic_persistence), "", "**接下来做什么**", *_actions(report)]
    return "\n".join(lines)


def _screen_switch_lines(report: MomentumReport) -> list[str]:
    """顶部负筛的动态开关：每个设计后面紧跟它的环移负控制区间。

    控制区间必须和设计印在一起。只印设计那一行，``diff_t``=+2.84 会被读成通过，
    而实测环移控制能到 +3.63——把对齐关系打乱反而更高。差值与择时项两个区间都印，
    因为环移不保留走前开启率，只有择时项是开启率中性的。
    """
    if not report.screen_switches:
        return []
    out = [
        "",
        "| 顶部负筛的动态开关 | 天数 | 差值 | 差值t | 机械项 | 择时项 | 价差t | 开启率 | 环移控制差值区间 | 判定 |",
        "| --- | --: | --: | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for design, controls in report.screen_switches:
        gap = switch_control_gap(design, controls)
        band = (
            "—"
            if gap.get("control_diff_min") is None
            else f"[{gap['control_diff_min']:+.3f}, {gap['control_diff_max']:+.3f}]"
            f"（最高 t={_num(gap.get('control_diff_t_max'), 2)}）"
            f"；择时项 [{_num(gap.get('control_timing_min'))}, {_num(gap.get('control_timing_max'))}]"
        )
        out.append(
            f"| {design.label} | {design.days} | {_num(design.diff)} | {_num(design.diff_t, 2)} | "
            f"{_num(design.mechanical)} | {_num(design.timing)} | {_spread_t_cell(design)} | "
            f"{_pct(design.on_rate)} | {band} | {gap.get('verdict', '—')} |"
        )
    out.append(
        "　注：控制组是把状态序列整体**环移**后重跑同一走前切换——取值分布与自相关保留，"
        "只打断它与同日前向收益的对齐关系，而待检的正是这条对齐。不能用均匀随机状态当"
        "控制：随机序列没有自相关，走前阈值取历史中位数会把开启率钉在 ~0.5，而真设计是"
        "0.88。环移**不保留**走前开启率（扩张窗口中位数对漂移序列路径依赖，实测控制落在"
        "0.37~0.79），所以判定同时看差值与择时项——后者已扣掉机械项（关闭率 × 全期价差），"
        "是开启率中性的那一半，要判「有信息」得两个口径都出圈。"
    )
    return out


def _window_line(window: dict[str, int] | None) -> str:
    """报告头写实际评估区间，而不是取数起点。

    飞书推送和落盘表格只有这一行能说清样本范围。缺了它，读者会拿工作流里的
    ``--start`` 当区间读——预热吃掉 120 个交易日，402 天的取数只评估了 271 天，
    而两个长度给的是相反结论（+0.070 t=+0.28 vs -0.320 t=-2.08）。
    """
    if not window:
        return "　样本区间：未记录（产物缺 eval_window，别拿 --start 当区间读）"
    return (
        f"　样本：行情自 {window['market_start']} 起 {window['market_days']} 天，"
        f"扣预热 {PROD_RPS_WINDOW_SLOW} 与 H+1 后**实际评估 "
        f"{window['eval_start']}~{window['eval_end']}，{window['eval_days']} 天**"
    )


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
    out.extend(_top_screen_actions(report))
    out.append("- ⑦ 任何参数改动落地前，须按本脚本 walk_forward_switch 的走前口径复验，且增益需大于成本 0.202%。")
    return out


def _top_screen_actions(report: MomentumReport) -> list[str]:
    """顶部负筛的两条读法：静态值不值得做，以及能不能跟着市场动态开关。

    静态那条的判据是**不重叠相位**，不是全样本 t。实测 H=10 全样本 t=+3.06，
    而 11 个相位全部落在 +0.59~+1.29，0/11 到 2——相邻日的 H 日前向收益共用 H-1
    天，全样本口径会把 t 夸大一倍以上。
    """
    out = [_static_screen_action(report)]
    if not report.screen_switches:
        out.append("- ⑥ 顶部负筛的动态开关未评估（样本不足或未接入）。")
        return out
    escaped = [
        (design, gap)
        for design, controls in report.screen_switches
        if not (gap := switch_control_gap(design, controls)).get("inside_control", True)
        and gap.get("verdict") != "样本不足"
    ]
    if not escaped:
        out.append(
            "- ⑥ 顶部负筛的所有动态开关设计，差值或择时项都落在环移负控制区间内——把状态与"
            "收益的对齐关系打乱后能拿到同样甚至更高的值，说明「跟着市场近期动量梯度开关负筛」"
            "**不含可用信息**。择时项那一栏已扣掉机械项，是开启率中性的口径，"
            "所以这不是控制组开启率不同造成的。维持静态口径，不要按这一族设计上线。"
        )
        return out
    names = "、".join(
        f"{design.label}（差值 {gap['design_diff']:+.3f}pct，控制区间 "
        f"[{gap['control_diff_min']:+.3f}, {gap['control_diff_max']:+.3f}]）"
        for design, gap in escaped
    )
    out.append(
        f"- ⑥ {names} 的差值与择时项都超出了环移负控制区间，与第四轮结论不同。"
        "下一步看它的**价差 t 相位区间**是否整体过 2；只有出圈不足以上线。"
    )
    return out


def _static_screen_action(report: MomentumReport) -> str:
    """静态顶部负筛相对生产闸门的增量。"""
    prod = next((s for s in report.thresholds if s.is_production), None)
    screen = report.top_screen
    if prod is None or screen is None or prod.excess is None or screen.excess is None:
        return "- ⑤ 顶部负筛样本不足，无法与生产闸门对照。"
    gain = screen.excess - prod.excess
    return (
        f"- ⑤ 闸门内剔掉顶部（任一腿 >= {TOP_SCREEN_PCT:.0f} 分位）超额 {screen.excess:+.3f}pct"
        f"（t={_num(screen.excess_t, 2)}），比生产闸门 {prod.excess:+.3f}pct 高 {gain:+.3f}pct。"
        "**判据看上表动态开关那一节的价差 t 相位区间，不要拿全样本 t 读**——"
        "相邻日的前向收益重叠，全样本口径会把 t 夸大一倍以上。"
    )


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
