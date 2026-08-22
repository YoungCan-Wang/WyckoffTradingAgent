"""按自定义形态口径检验前瞻收益：某类票「抓到了」到底赚不赚钱。

与 scripts/diagnose_funnel_recall.py 互补——那个按代码逐票回答「为什么没进候选池」，
这个按形态口径回答「这类票值不值得进」。先用它确认形态有正 alpha，再去动通道门槛，
否则放宽召回只会引入更多负 alpha 的标的。

2026-08-22 首轮结论（498 个交易日 / 2.3 万只次 / 扣 0.202% 往返成本）：
「前一日平淡、当日大涨、次日未高开」这类票，隔夜后是负 alpha，且**收紧阈值更差**：

    开盘缺口<=  当日涨幅>=  日均只数   净收益   净超额   为正日
        4%         7%         84     -0.27%   -0.71     39%
        3%         8%         60     -0.38%   -0.82     41%
        2%        10%         28     -0.55%   -0.98     39%

九档全负且单调恶化——当日涨幅要求越高，次日接力概率越低（动能已在当日释放）。
对照同期全市场（20 日均额 >= 8000 万）T+5 为 +0.44%，即买这批票不如随便买。

同时发现钱在日内而非隔夜：同一批票 T 日开盘买、T 日收盘卖为 +10.35%、276 天 100% 为正。
但那是**未来函数**——用「收盘涨幅」筛票再算「开盘到收盘」收益，实盘开盘时并不知道
哪只会收在高位。要验证「开盘 30 分钟能否预判」需历史分钟数据，而 TickFlow 的
get_intraday 只返回当日 241 根，取不到历史，故该路径当前无法回测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

# 与 core.trade_friction 对齐的 A 股单次往返成本。
ROUND_TRIP_COST_PCT = 0.202
MIN_DAYS = 20
MIN_HITS_PER_DAY = 3
# 为正日占比落在此区间视为无方向性，避免把噪声当信号。
RANDOM_BAND = (45.0, 55.0)


@dataclass(frozen=True)
class PatternSpec:
    """形态口径。默认值即 2026-08-22 首轮所用的基线。"""

    prev_day_max_pct: float = 3.0
    """T-1 日涨幅上限：筛「前一日平淡或水下」。"""

    open_gap_max_pct: float = 4.0
    """T 日开盘缺口上限：筛「次日未高开、仍可介入」。"""

    day_return_min_pct: float = 7.0
    """T 日涨幅下限：筛「当日大涨」。"""

    min_avg_amount_wan: float = 8000.0
    """T-1 及之前 20 日均成交额下限（万元），与生产 RISK_OFF 档门槛对齐。"""

    horizons: tuple[int, ...] = (5, 10)
    """持有期（交易日）。买点固定为 T+1 开盘——漏斗收盘后才出信号。"""

    def describe(self) -> str:
        return (
            f"T-1涨幅<{self.prev_day_max_pct:g}% / T开盘<={self.open_gap_max_pct:g}% / "
            f"T涨幅>{self.day_return_min_pct:g}% / 20日均额>={self.min_avg_amount_wan:g}万"
        )


@dataclass
class HorizonResult:
    horizon: int
    days: int
    avg_hits: float
    net_return_pct: float | None
    market_return_pct: float | None
    positive_day_pct: float | None

    @property
    def net_excess_pct(self) -> float | None:
        if self.net_return_pct is None or self.market_return_pct is None:
            return None
        return self.net_return_pct - self.market_return_pct

    @property
    def verdict(self) -> str:
        excess = self.net_excess_pct
        if self.days < MIN_DAYS or excess is None:
            return "样本不足"
        if self.positive_day_pct is not None and RANDOM_BAND[0] <= self.positive_day_pct <= RANDOM_BAND[1]:
            return "无方向性"
        return "正贡献" if excess > 0 else "负贡献"

    @property
    def actionable(self) -> bool:
        """净超额是否大于往返成本。不过门槛就不该据此放宽通道门槛。"""
        excess = self.net_excess_pct
        return excess is not None and excess > ROUND_TRIP_COST_PCT

    def as_dict(self) -> dict[str, object]:
        return {
            "horizon": self.horizon,
            "days": self.days,
            "avg_hits": round(self.avg_hits, 1),
            "net_return_pct": _round(self.net_return_pct),
            "market_return_pct": _round(self.market_return_pct),
            "net_excess_pct": _round(self.net_excess_pct),
            "positive_day_pct": _round(self.positive_day_pct, 1),
            "verdict": self.verdict,
            "actionable": self.actionable,
        }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class PatternReport:
    spec: PatternSpec
    results: list[HorizonResult] = field(default_factory=list)

    def at(self, horizon: int) -> HorizonResult | None:
        return next((r for r in self.results if r.horizon == horizon), None)

    @property
    def any_actionable(self) -> bool:
        return any(r.actionable for r in self.results)

    def as_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec.describe(),
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "any_actionable": self.any_actionable,
            "results": [r.as_dict() for r in self.results],
        }


def summarize_horizon(horizon: int, daily: list[dict[str, float]]) -> HorizonResult:
    """按交易日等权汇总。命中个股多的日子不该主导均值。"""
    usable = [
        row
        for row in daily
        if row.get("net") is not None and row.get("market") is not None and (row.get("hits") or 0) >= MIN_HITS_PER_DAY
    ]
    if len(usable) < MIN_DAYS:
        return HorizonResult(horizon, len(usable), 0.0, None, None, None)
    diffs = [float(r["net"]) - float(r["market"]) for r in usable]
    return HorizonResult(
        horizon=horizon,
        days=len(usable),
        avg_hits=mean(float(r.get("hits") or 0) for r in usable),
        net_return_pct=mean(float(r["net"]) for r in usable),
        market_return_pct=mean(float(r["market"]) for r in usable),
        positive_day_pct=100.0 * sum(1 for d in diffs if d > 0) / len(diffs),
    )
