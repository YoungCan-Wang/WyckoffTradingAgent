"""漏斗产出的选股效果检验：配对对照 + 随机负控制。

与既有评估脚本的分工
--------------------
``evaluate_gated_regime_candidates.py`` 按 regime 切分候选，每档只剩 8~15 天，
过不了 ``MIN_DAYS=20``；且它的基准是「同日全市场等权」。在 2026-06~08 这段
样本里全市场本身大跌，用它当基准会把「跟跌少一点」读成选股能力
（full-market-control-confounds-momentum 记的坑）。

本模块换两处口径：

1. **不按 regime 切**，先回答「漏斗整体有没有选股能力」。regime 只作为分组
   附注，不作为主结论的切分维度。
2. **对照组用「T 日已知 20 日涨幅最近邻 1:1 无放回配对」的非候选股**。候选
   天然偏高动量，全市场等权对照会把动量的 beta 混进来。配对后残差动量应接近 0，
   报告里给出实测值供核对。
3. **随机负控制**：每天从「与候选同动量分位区间」随机抽同样只数。配对超额若
   落在多种子随机控制的区间内，说明它只是「站在了那个动量位置上」，不含选股
   信息。这一条照 momentum_regime_eval 的做法固化进每次体检，不可省。

买点 T+1 开盘（漏斗信号收盘后产出，最早可成交是次日开盘），卖点 T+1+H 收盘，
扣 ROUND_TRIP_COST_PCT=0.202%，按交易日等权汇总。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from core.pattern_forward_eval import ROUND_TRIP_COST_PCT

# 每日最少命中数，低于此的日子不进汇总（单只票的日子噪声过大）。
MIN_HITS_PER_DAY = 3
# 最少交易日数。低于此只报样本量，不下判定。
MIN_DAYS = 20
# 随机负控制的种子。多种子是为了看边缘是否稳定，单种子的一次抽样不足以判定。
CONTROL_SEEDS = (11, 23, 37, 53, 71)
# 配对时允许的 20 日动量绝对偏差上限（百分点）。超出即视为无可配对对象。
MOM_MATCH_TOL_PCT = 3.0


def tstat(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    avg = mean(values)
    var = sum((v - avg) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return None
    return avg / ((var / len(values)) ** 0.5)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class GroupStat:
    """一组标的（候选 / 配对对照 / 随机控制）的逐日汇总。"""

    label: str
    days: int
    avg_size: float
    net_pct: float | None
    control_pct: float | None
    excess_pct: float | None
    excess_t: float | None
    positive_day_pct: float | None
    residual_mom_pct: float | None
    by_quarter: dict[int, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "avg_size": _round(self.avg_size, 1),
            "net_pct": _round(self.net_pct),
            "control_pct": _round(self.control_pct),
            "excess_pct": _round(self.excess_pct),
            "excess_t": _round(self.excess_t, 2),
            "positive_day_pct": _round(self.positive_day_pct, 1),
            "residual_mom_pct": _round(self.residual_mom_pct, 3),
            "by_quarter": {str(q): _round(v, 3) for q, v in sorted(self.by_quarter.items())},
        }


def summarize_group(label: str, daily: list[dict[str, float]]) -> GroupStat:
    """按交易日等权汇总。命中个股多的日子不该主导均值。"""
    usable = [
        row
        for row in daily
        if row.get("net") is not None and row.get("control") is not None and (row.get("size") or 0) >= MIN_HITS_PER_DAY
    ]
    if len(usable) < MIN_DAYS:
        return GroupStat(label, len(usable), 0.0, None, None, None, None, None, None)
    nets = [float(r["net"]) for r in usable]
    diffs = [float(r["net"]) - float(r["control"]) for r in usable]
    moms = [float(r["residual_mom"]) for r in usable if r.get("residual_mom") is not None]
    by_quarter: dict[int, list[float]] = {}
    for row, diff in zip(usable, diffs, strict=True):
        by_quarter.setdefault(_quarter_of(str(row["date"])), []).append(diff)
    return GroupStat(
        label=label,
        days=len(usable),
        avg_size=mean(float(r.get("size") or 0) for r in usable),
        net_pct=mean(nets),
        control_pct=mean(float(r["control"]) for r in usable),
        excess_pct=mean(diffs),
        excess_t=tstat(diffs),
        positive_day_pct=100.0 * sum(1 for d in diffs if d > 0) / len(diffs),
        residual_mom_pct=mean(moms) if moms else None,
        by_quarter={q: mean(v) for q, v in by_quarter.items()},
    )


def _quarter_of(ds: str) -> int:
    """'2026-08-31' -> 20263。用于看超额是否只来自某一个季度。"""
    year, month = int(ds[:4]), int(ds[5:7])
    return year * 10 + (month - 1) // 3 + 1


def match_by_momentum(
    hits: list[str],
    pool: list[str],
    mom: dict[str, float],
    *,
    tol_pct: float = MOM_MATCH_TOL_PCT,
) -> list[tuple[str, str]]:
    """按 T 日已知的 20 日涨幅做 1:1 无放回最近邻配对。

    只用 T 日及之前的数据算动量，不含任何前视。无放回是为了避免少数「动量正好
    落在候选密集区」的对照股被反复选中而放大它自身的特异噪声。偏差超过
    ``tol_pct`` 视为找不到可比对象，该候选**不进入配对样本**——宁可少算几只，
    也不要拿动量差 10 个点的票当对照。
    """
    avail = sorted((mom[c], c) for c in pool if c in mom)
    pairs: list[tuple[str, str]] = []
    for code in sorted(hits, key=lambda c: mom.get(c, 0.0)):
        if code not in mom or not avail:
            continue
        target = mom[code]
        best_i = min(range(len(avail)), key=lambda i: abs(avail[i][0] - target))
        if abs(avail[best_i][0] - target) > tol_pct:
            continue
        pairs.append((code, avail.pop(best_i)[1]))
    return pairs


def sample_momentum_band(
    hits: list[str],
    pool: list[str],
    mom: dict[str, float],
    *,
    seed: int,
    date: str,
    tol_pct: float = MOM_MATCH_TOL_PCT,
) -> list[str]:
    """随机负控制：为每只候选在「同动量邻域内」随机抽一只非候选股。

    第一版从候选动量的 [min, max] 区间里均匀抽，实测控制组残差动量 +6.1~+8.8pct
    ——候选动量分布右偏，均匀抽必然系统性偏低，那样的控制组不是干净对照，而是
    一个动量低 8 个点的更弱对手，它的「超额」里混着动量差，拿来跟配对超额比是
    错的。改成逐只在 ±tol_pct 邻域内随机替换，让控制组的残差动量同样归零：这样
    控制组与配对组唯一的差别只是「邻域内选哪一只」——随机 vs 漏斗。

    无放回，理由同 match_by_momentum。种子按 (seed, date) 混合，避免所有日子
    共用一次抽样序列。
    """
    rng = random.Random(f"{seed}:{date}")
    avail = sorted((mom[c], c) for c in pool if c in mom)
    picked: list[str] = []
    for code in sorted(hits, key=lambda c: mom.get(c, 0.0)):
        if code not in mom or not avail:
            continue
        target = mom[code]
        lo = _bisect_left(avail, target - tol_pct)
        hi = _bisect_left(avail, target + tol_pct)
        if hi <= lo:
            continue
        idx = rng.randrange(lo, hi)
        picked.append(avail.pop(idx)[1])
    return picked


def _bisect_left(pairs: list[tuple[float, str]], value: float) -> int:
    lo, hi = 0, len(pairs)
    while lo < hi:
        mid = (lo + hi) // 2
        if pairs[mid][0] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo


@dataclass
class Panels:
    """行情面板。open/close 按日索引，liquid 是当日流动性池，mom20 是 T 日已知 20 日涨幅。"""

    open: dict[str, dict[str, float]]
    close: dict[str, dict[str, float]]
    liquid: dict[str, set[str]]
    mom20: dict[str, dict[str, float]]
    dates: list[str]

    def window(self, signal_ds: str, horizon: int) -> tuple[str, str] | None:
        """T+1 开盘买、T+1+horizon 收盘卖。窗口越界返回 None。"""
        if signal_ds not in self.dates:
            return None
        idx = self.dates.index(signal_ds)
        buy_i, sell_i = idx + 1, idx + 1 + horizon
        if sell_i >= len(self.dates):
            return None
        return self.dates[buy_i], self.dates[sell_i]

    def gross_return(self, codes: list[str], buy_ds: str, sell_ds: str) -> float | None:
        """等权毛收益（%），未扣成本。"""
        opens, closes = self.open.get(buy_ds, {}), self.close.get(sell_ds, {})
        rets = []
        for code in codes:
            o, c = opens.get(code), closes.get(code)
            if o and c and o > 0:
                rets.append(100.0 * (c / o - 1.0))
        return mean(rets) if rets else None


def resolve_layer(day: dict, universe: set[str], layer: str) -> tuple[list[str], list[str]]:
    """把「测哪一层」翻成 (待测组, 对照池)。

    - ``formal_l4`` / ``all``：对照池是全市场里的非候选股，答「漏斗产出 vs 场内同侪」。
    - ``l4_vs_rest``：对照池是宽池内**未进 L4** 的候选，答「L4 这道筛本身有没有用」。
      这一层最能隔离筛的贡献：两组都已过了宽池入口，差别只在 L4。
    """
    wide = set(day.get("all") or []) & universe
    l4 = set(day.get("formal_l4") or []) & universe
    if layer == "l4_vs_rest":
        return sorted(l4), sorted(wide - l4)
    hits = sorted(l4 if layer == "formal_l4" else wide)
    return hits, sorted(universe - wide)


def evaluate_daily(
    cands: dict[str, dict],
    panels: Panels,
    horizon: int,
    *,
    status: str = "formal_l4",
    seeds: tuple[int, ...] = CONTROL_SEEDS,
) -> dict[str, list[dict]]:
    """逐日算候选、配对对照、各随机控制的收益。

    成本对候选和对照同样扣：两边都是一次往返，比较的是选股而非交易频率。
    """
    rows: dict[str, list[dict]] = {"matched": [], **{f"control_{s}": [] for s in seeds}}
    for ds in sorted(cands):
        win = panels.window(ds, horizon)
        if win is None:
            continue
        buy_ds, sell_ds = win
        universe = panels.liquid.get(ds, set())
        mom = panels.mom20.get(ds, {})
        hits, pool = resolve_layer(cands[ds], universe, status)
        if len(hits) < MIN_HITS_PER_DAY:
            continue

        # 随机控制必须与配对组用同一批候选（paired_hits），否则两者分母不同、
        # 超额不可直接比较——这是把「配对超额 vs 随机超额」摆在一起的前提。
        pairs = match_by_momentum(hits, pool, mom)
        if len(pairs) < MIN_HITS_PER_DAY:
            continue
        paired_hits = [p[0] for p in pairs]
        paired_ctrl = [p[1] for p in pairs]
        hit_ret = panels.gross_return(paired_hits, buy_ds, sell_ds)
        if hit_ret is None:
            continue

        ctl = panels.gross_return(paired_ctrl, buy_ds, sell_ds)
        if ctl is not None:
            rows["matched"].append(
                {
                    "date": ds,
                    "size": len(pairs),
                    # 两边都扣成本，故超额里成本抵消；net/control 本身仍是净值口径
                    "net": hit_ret - ROUND_TRIP_COST_PCT,
                    "control": ctl - ROUND_TRIP_COST_PCT,
                    "residual_mom": mean(mom[h] for h in paired_hits) - mean(mom[c] for c in paired_ctrl),
                }
            )

        for seed in seeds:
            band = sample_momentum_band(paired_hits, pool, mom, seed=seed, date=ds)
            if len(band) < MIN_HITS_PER_DAY:
                continue
            ctl = panels.gross_return(band, buy_ds, sell_ds)
            if ctl is None:
                continue
            rows[f"control_{seed}"].append(
                {
                    "date": ds,
                    "size": len(band),
                    "net": hit_ret - ROUND_TRIP_COST_PCT,
                    "control": ctl - ROUND_TRIP_COST_PCT,
                    "residual_mom": mean(mom[h] for h in paired_hits if h in mom)
                    - mean(mom[c] for c in band if c in mom),
                }
            )
    return rows


def control_gap(matched: GroupStat, controls: list[GroupStat]) -> dict[str, Any]:
    """配对超额相对随机负控制的差距。

    控制组每天从「候选动量区间内」随机抽同样只数，只带动量选位这一条信息。
    配对超额若落在控制组区间内、或差距小于控制组自身的抽样宽度，就不能说漏斗
    含选股信息——这是唯一能否掉「漏斗有效」的环节。
    """
    usable = [c.excess_pct for c in controls if c.excess_pct is not None]
    if matched.excess_pct is None or len(usable) < 2:
        return {"verdict": "样本不足", "seeds": len(usable)}
    avg = mean(usable)
    spread = max(usable) - min(usable)
    gap = matched.excess_pct - avg
    inside = min(usable) <= matched.excess_pct <= max(usable)
    return {
        "seeds": len(usable),
        "matched_excess": _round(matched.excess_pct),
        "control_excess_avg": _round(avg),
        "control_excess_min": _round(min(usable)),
        "control_excess_max": _round(max(usable)),
        "seed_spread": _round(spread),
        "gap": _round(gap),
        "verdict": (
            "配对超额落在随机负控制区间内：边缘仅来自动量选位，不含选股信息"
            if inside or gap <= spread
            else "配对超额跑赢随机负控制：含独立选股信息"
        ),
    }
