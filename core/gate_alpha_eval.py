"""门槛层 alpha 检验：L3 题材共振与止损参考价陈旧度。

两条都是 2026-08-20 首轮跑出的结论，脚本化的目的是让它们能被持续验证，
而不是靠单段行情拍板。

**L3 题材共振**（生产 ``top_n_sectors=5``）：按行业近 5 日动量取前 N 个「热门行业」，
只放行其中的标的。首轮 107 个交易日实测——

    topN=3   热门 T+5 -1.33%  非热门 -0.48%  差 -0.85pct
    topN=5   热门 -0.87%      非热门 -0.50%  差 -0.37pct   （生产值）
    topN=12  热门 -0.53%      非热门 -0.56%  差 +0.03pct
    topN=20  热门 -0.49%      非热门 -0.60%  差 +0.11pct

**越热越差**，即这一层在做负向筛选（追热点买在板块高位）。但放宽到 20 的增益
+0.11pct 小于单次往返成本 0.202%，改了在净收益上看不出来，故首轮未改。

**止损参考价陈旧度**：生产用 ``recent_high = high.tail(60).max()`` 作跟踪止损基准，
回撤 10% 即触发（core/wyckoff_engine.py 的 ``_compute_stop_loss``）。深跌股的参考价会
长期远高于现价，于是永久处于「已破位」——江顺科技 2026-08-18 参考价 119.01 而收盘
78.13（偏离 +52%），次日却涨 10%。但按偏离分档实测——

    偏离 0~15%   触发后 T+5 超额 -0.51pct
    偏离 15~30%  超额 -0.54pct
    偏离 30~50%  超额 -0.35pct
    偏离 >50%    超额 -0.07pct

**四档全为负**，止损在统计上是对的；陈旧档只是最弱而非反向。江顺是个案不是规律，
故明确**不改风控**。这条检验保留下来是为了持续确认该结论，而不是为了推翻它。

口径约定：``excess`` 为相对同日全市场的超额。对止损而言**负值表示止损正确**
（卖出后确实跑输），正值表示卖早了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any

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
    label: str
    days: int
    avg_group_size: float
    inside_ret: float | None
    outside_ret: float | None
    excess: float | None
    positive_day_pct: float | None
    is_production: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "avg_group_size": round(self.avg_group_size, 1),
            "inside_ret": _round(self.inside_ret),
            "outside_ret": _round(self.outside_ret),
            "excess": _round(self.excess),
            "positive_day_pct": _round(self.positive_day_pct, 1),
            "is_production": self.is_production,
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.excess is None:
            return "样本不足"
        if self.positive_day_pct is not None and 45.0 <= self.positive_day_pct <= 55.0:
            return "为正日占比接近随机：无方向性"
        return "正贡献" if self.excess > 0 else "负贡献"


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
                "excess 为相对同日全市场的超额。题材层正值=该层有效；"
                "止损档**负值=止损正确**（卖出后确实跑输），正值=卖早了。"
            ),
        }


def summarize(label: str, daily: list[dict[str, float]], *, is_production: bool = False) -> GateStat:
    """把逐日观测汇总成一档统计。每日等权，避免个股数量多的日子主导均值。"""
    usable = [row for row in daily if row.get("inside") is not None and row.get("outside") is not None]
    if len(usable) < MIN_DAYS:
        return GateStat(label, len(usable), 0.0, None, None, None, None, is_production)
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
        "| 题材共振 topN | 天数 | 日均入选 | 热门 | 非热门 | 差值 | 为正日% | 判定 |",
        "| --- | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for stat in report.theme:
        lines.append(_row(stat, mark_production=stat.is_production))
    lines += [
        "",
        "| 止损参考价偏离 | 天数 | 日均触发 | 触发后 | 市场 | 超额 | 为正日% | 判定 |",
        "| --- | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for stat in report.stop_loss:
        lines.append(_row(stat))
    lines += [
        "",
        "**读法**　题材层：差值为正才说明「只买热门行业」有效。"
        "止损档：**超额为负 = 止损正确**（卖出后确实跑输大盘），为正才说明卖早了。",
        "",
        "**接下来做什么**",
        _theme_action(report.theme),
        _stop_action(report.stop_loss),
        "- 任一结论要落到参数改动，需先确认增益大于单次往返成本 0.202%，且跨越多个行情段后方向稳定。",
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
    prod = next((s for s in stats if s.is_production), None)
    if prod is None or prod.excess is None:
        return "- ① 题材层样本不足，继续积累。"
    if prod.excess < 0:
        best = max((s for s in stats if s.excess is not None), key=lambda s: s.excess, default=None)
        gain = None if best is None else best.excess - prod.excess
        tail = "" if gain is None else f"；放宽到 {best.label} 可得 {gain:+.2f}pct"
        return (
            f"- ① 题材共振为负贡献（{prod.excess:+.2f}pct），即「只买热门行业」在做反向筛选{tail}。"
            "增益若小于成本 0.202% 则不值得改。"
        )
    return f"- ① 题材共振为正贡献（{prod.excess:+.2f}pct），维持现状。"


def _stop_action(stats: list[GateStat]) -> str:
    negatives = [s for s in stats if s.excess is not None and s.excess < 0]
    if len(negatives) == len([s for s in stats if s.excess is not None]) and negatives:
        return "- ② 止损各偏离档超额全为负，说明止损触发后确实继续跑输——**不要因为个别陈旧参考价的反例去放宽风控**。"
    positives = [s.label for s in stats if s.excess is not None and s.excess > 0]
    return f"- ② 止损在这些偏离档上为正超额（卖早了）：{'、'.join(positives)}——值得单独复核该档判定。"
