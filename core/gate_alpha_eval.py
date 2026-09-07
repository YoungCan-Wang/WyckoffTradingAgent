"""门槛层 alpha 检验：L3 题材共振与止损参考价陈旧度。

**2026-09-07 修正：首轮那两条结论是裸差值算出来的，没有任何对照。**首轮（2026-08-20）
把「热门 − 非热门」和「触发档 − 全市场」的日均差直接读成正/负贡献，而两侧的动量分布
差得很远：题材层的「热门」按定义就是成分股 5 日动量均值最高的行业，止损层的「深偏离」
按定义就是从 60 日高点跌得最多的票，对照侧还是**未做任何匹配**的全市场均值。两条都踩在
「全市场对照混入动量」上。

补做逐对随机互换否证（``core/funnel_effect_eval.swap_falsification``，配对钉住、只抹标签，
动量按最近邻配平）。下面每条结论都注明自己的评估区间——**这一层的读数对窗口极其敏感，
不带区间的数字没有意义**。

**止损参考价陈旧度——原结论不成立。** 142 个交易日、评估区间 2026-01-28..2026-08-28，
配对后残差动量 +0.0085pct/18686 对。「未触发 vs 已触发」配平动量后：胜率差 +0.041pct
（双侧 p=0.821，落在互换零分布内），收益差 -0.027pct。即止损这个标签在配平个股动量后
**不含信息**，首轮那 4 档 -0.07~-0.54pct 的超额来自未匹配的全市场基准。收紧容差到 0.5
同样落带内（胜率 -0.065，p=0.751）。四档各自单独跑（对照池限定在**该档内**，回答「这一档
触发得对不对」）方向一致：2026-04..08 那 99 天里四档胜率差分别 -0.016 / -0.165 / -1.159 /
-1.019，**全为非正**，即没有任何一档显示「未触发的那批本该留着」。

原来写在这里的「四档全为负，止损在统计上是对的……故明确不改风控」**已作废**：不是说
该放宽风控，而是「不放宽」这个决定目前没有测量支撑，别再引用这条当依据。

**L3 题材共振——过得了逐日的零分布，过不了跨窗口的稳定性检验。**「非热门 vs 热门」
配平动量后，生产档 topN=5、评估区间 2026-01-05..2026-08-28 共 159 天，日均 225.5 对：
胜率差 +3.064pct（双侧 p=0.005），收益差 +0.488pct。但**按 2 个月切开，符号在同一段
八个月里反了号**：

===============  ====  ==========  =====
区间             天数  胜率差 pct  p
===============  ====  ==========  =====
2026-01 ~ 02       34      +9.087  0.005
2026-03 ~ 04       43      +7.932  0.005
2026-05 ~ 06       39      -4.468  0.005
2026-07 ~ 08       43      +0.199  0.851
汇总              159      +3.064  0.005
===============  ====  ==========  =====

四段之间 mean +3.188 / sd 6.451，**t=+0.988（n=4），跨窗口不显著**。三段非空 p 全部钉在
200 次置换的下限 1/201≈0.005 上，说明「打得过自己那天的零分布」很容易，真正有区分力的是
段间那个离散度。所以汇总那个 +3.064 **不能当作可用的边缘**：换一个窗口它就换一个符号——
同一套代码，topN=5 在 142 天读 +2.808、159 天读 +3.064、而 2026-04..08 那 99 天读
**-1.739**（后者正好是 05~06 负段主导的窗口）；topN=3 在 142 天读 +4.845。这些数彼此不
矛盾，它们是同一件事的不同切法。

这一族与已在案的两条同形：动量梯度按半年反号、池子相对优势按月翻号。**结论是「这个标签
目前没有稳定方向」，不是「放宽 topN 可得 +3pct」。**

容差从 3.0 收到 0.5 在这个几何下**几乎没有区分力**（非热门池约 4000 只、热门篮约 140 只，
最近邻距离本就趋 0，配对数 132.5→130.6），不要引用它当稳健性证据。

**这一层量的不是生产那道闸。** 本模块按「全市场个股 5 日动量的行业均值」取前 N，生产
（``core/wyckoff_engine.py`` 的 ``_compute_per_sector_strength``）取的是**候选内**成分股复合
截面百分位（ret20 40% + ret5 30% + ret3 30%）的**中位数**；且 L3 放行是四路取或——在
``top_sectors`` 内、或在 ``keep_sectors`` 内且个股强度达标、或在热门概念内且强度达标、或
individual 强度单独达标，末尾还有 ``len(filtered) < 3`` 兜底放行全量。所以 topN 网格这里的
读数**不能直接换算成生产参数**，它是「按行业动量分组」这个构造的性质，不是那道闸的性质。

口径约定：``excess`` 是裸日均差，**描述性**的，两侧动量未配平，不能当结论；判定一律看
``swap``（动量配平后的逐对互换否证）。``swap`` 的待测组是**要动的那一侧**（题材层=非热门，
止损层=未触发），观测为正才说明那个方向可行；它与 ``excess`` 的符号天然相反。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from core.funnel_effect_eval import SwapTest

# 生产值，用于在报告里标注「当前档位」。
PROD_TOP_N_SECTORS = 5
PROD_TRAILING_DRAWDOWN_PCT = -10.0
PROD_RECENT_HIGH_WINDOW = 60

TOP_N_GRID = (3, 5, 8, 12, 20)
STALE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 15.0, "0~15%"),
    (15.0, 30.0, "15~30%"),
    (30.0, 50.0, "30~50%"),
    (50.0, float("inf"), ">50%"),
)
MIN_DAYS = 5
MIN_GROUP = 3


@dataclass
class GateStat:
    """一档门槛的读数。

    ``excess`` 是裸日均差，只作描述；判定看 ``swap``。``swap_question`` 写清待测组是
    哪一侧——它与 ``excess`` 的符号相反，不写的话读者会把同一个发现的两面当成矛盾。
    """

    label: str
    days: int
    avg_group_size: float
    inside_ret: float | None
    outside_ret: float | None
    excess: float | None
    positive_day_pct: float | None
    is_production: bool = False
    swap_question: str | None = None
    swap: dict[str, SwapTest] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "avg_group_size": round(self.avg_group_size, 1),
            "inside_ret": _round(self.inside_ret),
            "outside_ret": _round(self.outside_ret),
            "excess": _round(self.excess),
            "excess_is_controlled": False,
            "positive_day_pct": _round(self.positive_day_pct, 1),
            "is_production": self.is_production,
            "swap_question": self.swap_question,
            "swap": None if not self.swap else {col: test.as_dict() for col, test in self.swap.items()},
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        """没有对照就不给方向性结论——裸差值两侧动量没配平，说不了「贡献」。"""
        if self.days < MIN_DAYS or self.excess is None:
            return "样本不足"
        if self.swap is None:
            return f"裸差值 {self.excess:+.2f}pct（未配平动量，仅描述，不是结论）"
        win = self.swap.get("stock_win")
        ret = self.swap.get("gross_return")
        if win is None:
            return "对照未跑出结果：尚未可知"
        head = "" if not self.swap_question else f"{self.swap_question} → "
        tail = "" if ret is None else f"；收益栏 {ret.observed_pct:+.3f}pct（双侧 p={ret.p_two_sided:.3f}）"
        return f"{head}{win.verdict}{tail}"


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class GateReport:
    theme: list[GateStat] = field(default_factory=list)
    stop_loss: list[GateStat] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "theme_resonance": [stat.as_dict() for stat in self.theme],
            "stop_loss_staleness": [stat.as_dict() for stat in self.stop_loss],
            "production": {
                "top_n_sectors": PROD_TOP_N_SECTORS,
                "trailing_drawdown_pct": PROD_TRAILING_DRAWDOWN_PCT,
                "recent_high_window": PROD_RECENT_HIGH_WINDOW,
            },
            "reading": (
                "excess 是裸日均差，两侧动量未配平，**仅描述不是结论**——题材层的「热门」按定义就是"
                "成分股动量均值最高的行业，止损层的对照侧是未匹配的全市场均值，两个差值里主要是动量。"
                "判定一律看 swap（动量配平后的逐对互换否证）：待测组是要动的那一侧（题材=非热门、"
                "止损=未触发），故 swap 的符号与 excess 天然相反。p 值偏乐观，持有窗口逐日重叠。"
            ),
        }


def summarize(
    label: str,
    daily: list[dict[str, float]],
    *,
    is_production: bool = False,
    swap_question: str | None = None,
    swap: dict[str, SwapTest] | None = None,
) -> GateStat:
    """把逐日观测汇总成一档统计。每日等权，避免个股数量多的日子主导均值。

    ``swap`` 缺省为 None：那样出来的 ``verdict`` 只会说「裸差值……不是结论」。判定要
    有方向性，调用方必须自己配平动量、跑完互换否证再传进来。
    """
    usable = [row for row in daily if row.get("inside") is not None and row.get("outside") is not None]
    if len(usable) < MIN_DAYS:
        return GateStat(label, len(usable), 0.0, None, None, None, None, is_production, swap_question, swap)
    diffs = [float(row["inside"]) - float(row["outside"]) for row in usable]
    return GateStat(
        label=label,
        days=len(usable),
        avg_group_size=mean(float(row.get("size") or 0) for row in usable),
        inside_ret=mean(float(row["inside"]) for row in usable),
        outside_ret=mean(float(row["outside"]) for row in usable),
        excess=mean(diffs),
        positive_day_pct=100.0 * sum(1 for value in diffs if value > 0) / len(diffs),
        is_production=is_production,
        swap_question=swap_question,
        swap=swap,
    )


def band_of(deviation_pct: float) -> str | None:
    """参考价偏离幅度归档。负偏离（参考价低于现价）不属于陈旧问题，返回 None。"""
    if deviation_pct < 0:
        return None
    for low, high, label in STALE_BANDS:
        if low <= deviation_pct < high:
            return label
    return None


def render(report: GateReport) -> str:
    lines = [
        "**门槛层 alpha 检验**",
        "",
        "| 题材共振 topN | 天数 | 日均入选 | 热门 | 非热门 | 裸差值 | 为正日% | 判定（配平动量后） |",
        "| --- | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for stat in report.theme:
        lines.append(_row(stat, mark_production=stat.is_production))
    lines += [
        "",
        "| 止损参考价偏离 | 天数 | 日均触发 | 触发后 | 市场（未匹配） | 裸差值 | 为正日% | 判定（配平动量后） |",
        "| --- | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for stat in report.stop_loss:
        lines.append(_row(stat))
    lines += [
        "",
        "**读法**　差值一栏是**裸日均差，两侧动量没配平，只作描述**：题材层的「热门」按定义就是成分股"
        "动量均值最高的行业，止损层的对照侧是未做匹配的全市场均值，这两个差值里主要是动量，"
        "**不能**读成「该层有效」或「止损正确」。判定看最后一栏（动量配平后的逐对互换否证），"
        "它的待测组是要动的那一侧（题材=非热门、止损=未触发），符号与差值栏天然相反。",
        "",
        "**接下来做什么**",
        _theme_action(report.theme),
        _stop_action(report.stop_loss),
        "- 任一结论要落到参数改动，需先确认增益大于单次往返成本 0.202%，且跨越多个行情段后方向稳定。"
        "互换否证的 p 值偏乐观——持有窗口逐日重叠、日间观测不独立，而置换零分布按独立处理。",
    ]
    return "\n".join(lines)


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _plain(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def _row(stat: GateStat, *, mark_production: bool = False) -> str:
    name = f"**{stat.label}（生产值）**" if mark_production else stat.label
    excess = "—" if stat.excess is None else f"{stat.excess:+.2f}"
    return (
        f"| {name} | {stat.days} | {stat.avg_group_size:.0f} | {_signed(stat.inside_ret)} | "
        f"{_signed(stat.outside_ret)} | {excess} | {_plain(stat.positive_day_pct)} | {stat.verdict} |"
    )


def _theme_action(stats: list[GateStat]) -> str:
    """只讲对照说了什么。裸差值的大小、以及「放宽到 topN=X 可得多少」都不能当行动依据：

    ① 差值两侧动量没配平；② 这个构造不是生产那道闸（见模块 docstring），topN 换不成
    生产参数。
    """
    prod = next((s for s in stats if s.is_production), None)
    if prod is None or prod.excess is None:
        return "- ① 题材层样本不足，继续积累。"
    if prod.swap is None:
        return (
            f"- ① 题材层裸差值 {prod.excess:+.2f}pct，**未配平动量**——「热门」按定义就是成分股动量均值最高的行业，"
            "这个差值里主要是动量。要给结论得先跑逐对互换否证。"
        )
    win = prod.swap.get("stock_win")
    if win is None:
        return "- ① 题材层对照未跑出结果，尚未可知。"
    if win.observed_pct <= 0 or win.p_two_sided > 0.05:
        return f"- ① 题材层配平动量后没通过：{win.verdict}。这一层维持现状，别拿裸差值去调 topN。"
    return (
        f"- ① 题材层配平动量后胜率差 {win.observed_pct:+.3f}pct（双侧 p={win.p_two_sided:.3f}）——"
        "**这只是打过了逐日的置换零分布，不等于可用**。2026-01..08 拆成 4 段时符号就反了号"
        "（+9.09 / +7.93 / -4.47 / +0.20，段间 t=+0.988 不显著），别默认这次的窗口不同。"
        "要落到改动得先满足两条：按 2 个月拆开每段同号、段间 t 显著；且本模块的行业动量分组"
        "不是生产 L3 那道闸（生产用候选内复合百分位中位数 + 四路取或），得在生产口径上重测。"
    )


def _stop_action(stats: list[GateStat]) -> str:
    """止损层同理。首轮「四档全为负 → 止损是对的」是把未匹配全市场当对照得出的，已作废。"""
    controlled = [s for s in stats if s.swap and s.swap.get("stock_win")]
    if not controlled:
        return (
            "- ② 止损各档只有裸差值，对照侧是**未做动量匹配的全市场均值**——「深偏离」按定义就是从 60 日高点"
            "跌得最多的票，这个超额里主要是动量。**不要**拿它当「止损正确」或「该放宽风控」的依据。"
        )
    passed = [s.label for s in controlled if (t := s.swap["stock_win"]).observed_pct > 0 and t.p_two_sided <= 0.05]
    if not passed:
        return (
            "- ② 止损各档配平动量后都没通过：这个标签不含信息，首轮「触发后确实继续跑输」的读数是全市场基准"
            "带来的动量差。风控要不要动，目前没有测量支撑——保持现状是默认，不是结论。"
        )
    return f"- ② 止损这些档配平动量后仍显示卖早了：{'、'.join(passed)}——值得单独复核，仍需按月拆开。"
