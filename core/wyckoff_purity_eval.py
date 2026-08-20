"""威科夫纯度检验：原生事件 vs 均线叠加，跨多个持有期。

起因是一个理论层面的质疑：威科夫原版方法里**没有均线概念**，而本仓 ``core/`` 里
``bias_200`` 出现 81 次、``ma_long`` 92 次、``ma_short`` 71 次、``above_ma50`` 33 次，
同时原版吸筹阶段的前四步——PS 初步支撑、SC 抛售高潮、AR 自动反弹、ST 二次测试——
一次都没实现。所以问题是：偏离原版的这部分，是正收益还是负收益。

2026-08-20 首轮跑数（498 个交易日 / 269 万行 / 5602 只，2024-08~2026-08）给出的答案是
**偏离方向是对的，补全原版会更亏**：

    事件                T+5      T+10     T+20     T+40
    SOS              +0.36    -0.10    -0.57    -0.89
    SOS+MA200        +0.43    -0.00    -0.41    -0.68   ← 唯一正贡献
    LPS              +0.01    -0.05    -0.07    -0.31
    Spring           -0.49    -0.66    -1.06    -0.81
    SC 抛售高潮       -0.71    -1.43    -2.12    -3.83   ← 原版缺失的事件，最差

三条结论：

1. **唯一正 alpha 是 SOS + MA200 + T+5**（+0.43、57% 的交易日为正）——这恰是最不威科夫、
   最趋势跟随、最短线的组合。且该 alpha 只存在于 T+5，T+10 即归零。
2. **补全原版是负收益**：实测 SC 抛售高潮 T+40 超额 -3.83pct、绝对收益 -6.27%。
   「抄底抛售高潮」在这两年 A 股明确亏钱。
3. **拉长持有期不能救 Spring**：T+5 -0.49 → T+20 -1.06，越拉越差。曾假设「威科夫是
   中长线方法所以 T+5 太短」，该假设被否。

另需区分 beta 与 alpha：LPS 与 Spring 的**绝对收益**随持有期上升（LPS T+40 +1.14%），
但超额为负——赚的是市场整体的钱，不是选股的钱。若目标是跑赢基准，它们无效。

一处此前的误判也记在这里：用 107 天数据曾测出「纯 Spring -0.10、加 MA20 变 -1.57」，
据此以为均线严重伤害 Spring。两年数据显示六档（纯/+MA20/+MA50/+MA200/+MA20&50/MA20下方）
全在 -0.36~-0.47 之间、几乎无差别——那个 -1.57 是日均命中仅 11 只造成的噪声。
**Spring 本身是负的，不是均线毁了它。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any

HORIZONS: tuple[int, ...] = (5, 10, 20, 40)
MIN_DAYS = 20
MIN_GROUP = 5
# 单次往返成本，用于判断增益是否值得落到参数改动。见 core/trade_friction.py。
ROUND_TRIP_COST_PCT = 0.202
# 为正日占比落在该区间内视为无方向性，避免把噪声当信号。
RANDOM_BAND = (45.0, 55.0)

# 原版吸筹阶段 A~E 的事件序列；标注哪些本仓已实现。
WYCKOFF_CANON: tuple[tuple[str, str, bool], ...] = (
    ("PS", "初步支撑", False),
    ("SC", "抛售高潮", False),
    ("AR", "自动反弹", False),
    ("ST", "二次测试", False),
    ("Spring", "弹簧/假跌破", True),
    ("LPS", "最后支撑点", True),
    ("SOS", "强势信号", True),
)


@dataclass
class HorizonStat:
    horizon: int
    days: int
    avg_hits: float
    event_ret: float | None
    market_ret: float | None
    excess: float | None
    positive_day_pct: float | None

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.excess is None:
            return "样本不足"
        if self.positive_day_pct is not None and RANDOM_BAND[0] <= self.positive_day_pct <= RANDOM_BAND[1]:
            return "无方向性"
        return "正贡献" if self.excess > 0 else "负贡献"

    @property
    def beats_cost(self) -> bool:
        """超额是否大于单次往返成本。不过门槛的正贡献不足以支撑参数改动。"""
        return self.excess is not None and self.excess > ROUND_TRIP_COST_PCT

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "days": self.days,
            "avg_hits": round(self.avg_hits, 1),
            "event_ret": _round(self.event_ret),
            "market_ret": _round(self.market_ret),
            "excess": _round(self.excess),
            "positive_day_pct": _round(self.positive_day_pct, 1),
            "verdict": self.verdict,
            "beats_cost": self.beats_cost,
        }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class EventCurve:
    """一个事件在各持有期上的表现曲线。"""

    name: str
    is_canon: bool
    stats: list[HorizonStat] = field(default_factory=list)

    def at(self, horizon: int) -> HorizonStat | None:
        return next((stat for stat in self.stats if stat.horizon == horizon), None)

    @property
    def best(self) -> HorizonStat | None:
        usable = [stat for stat in self.stats if stat.excess is not None]
        return max(usable, key=lambda stat: stat.excess) if usable else None

    @property
    def decays(self) -> bool:
        """短周期为正、长周期转负——说明 alpha 是短期效应，不能靠拉长持有期放大。"""
        short = self.at(HORIZONS[0])
        long_ = self.at(HORIZONS[-1])
        if short is None or long_ is None or short.excess is None or long_.excess is None:
            return False
        return short.excess > 0 > long_.excess

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_canon": self.is_canon,
            "decays_with_horizon": self.decays,
            "stats": [stat.as_dict() for stat in self.stats],
        }


def summarize_horizon(horizon: int, daily: list[dict[str, float]]) -> HorizonStat:
    """按交易日等权汇总。个股多的日子不该主导均值。"""
    usable = [row for row in daily if row.get("event") is not None and row.get("market") is not None]
    if len(usable) < MIN_DAYS:
        return HorizonStat(horizon, len(usable), 0.0, None, None, None, None)
    diffs = [float(row["event"]) - float(row["market"]) for row in usable]
    return HorizonStat(
        horizon=horizon,
        days=len(usable),
        avg_hits=mean(float(row.get("hits") or 0) for row in usable),
        event_ret=mean(float(row["event"]) for row in usable),
        market_ret=mean(float(row["market"]) for row in usable),
        excess=mean(diffs),
        positive_day_pct=100.0 * sum(1 for value in diffs if value > 0) / len(diffs),
    )


def canon_coverage() -> dict[str, Any]:
    """原版事件的实现覆盖率。回答「差了多少」这一半问题。"""
    implemented = [code for code, _, done in WYCKOFF_CANON if done]
    missing = [f"{code}({name})" for code, name, done in WYCKOFF_CANON if not done]
    return {
        "total": len(WYCKOFF_CANON),
        "implemented": implemented,
        "missing": missing,
        "coverage_pct": round(100.0 * len(implemented) / len(WYCKOFF_CANON), 1),
    }
