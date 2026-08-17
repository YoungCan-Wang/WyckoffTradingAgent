"""市场水温（regime）判定的前瞻检验：它到底在标底部还是标危险。

起因：2026-08-16 复盘发现 10 笔 EXIT/清仓建议里 9 笔卖在阶段底部（T+5 卖出后均值
+13.26%、继续下跌占 0%），其中 7/30 那批理由写的是「CRASH 环境下无保留价值，执行清仓」。
顺着测 12 次 CRASH 判定，结果是 T+5 +0.68%（基准 −0.44%）、超基准 +1.12pct、
单尾 p=0.015，且相对「纯近3日跌最多」对照仍有 +0.94pct 增量、重叠仅 5/12。

即 CRASH 更像**有效的底部识别**，而系统把它当「危险→清仓」用，方向用反了。

本模块把该检验固化成可复算流程，每月重跑以判断这是真信号还是 V 型反转的巧合：
- 每个 regime 判定日 → 主指数前瞻 N 日收益，对比「区间内任意一天买入」的基准。
- **纯跌幅对照**：另取「近 3 日累计跌幅最大」的同样天数做对照组。若两者表现相当且
  高度重叠，则 regime 没有独立信息，测到的只是众所周知的短期均值回复。
- **随机负控制**：从所有交易日随机抽同样天数 2000 次，得到差值分布与单尾 p。
样本不足时明确返回「样本不足」，不给结论。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

MIN_REGIME_DAYS = 8
RANDOM_ROUNDS = 2000
LOOKBACK_DAYS = 3


@dataclass
class RegimeStat:
    regime: str
    days: int
    forward: float | None
    excess: float | None
    positive_pct: float | None
    p_value: float | None = None
    random_ci: tuple[float | None, float | None] = (None, None)
    verdict: str = "样本不足"
    note: str = ""


@dataclass
class RegimeReport:
    horizon: int
    index_code: str
    window: tuple[str, str]
    baseline: float | None
    baseline_days: int
    stats: list[RegimeStat] = field(default_factory=list)
    drawdown_control: dict[str, Any] = field(default_factory=dict)


def forward_return_map(dates: list[str], closes: list[float], horizon: int) -> dict[str, float]:
    """交易日 -> 前瞻 horizon 日收益（百分数）。尾部不足 horizon 的日子直接不收录。"""
    out: dict[str, float] = {}
    for i in range(len(dates) - horizon):
        base = float(closes[i])
        if base <= 0:
            continue
        value = (float(closes[i + horizon]) / base - 1.0) * 100.0
        if not math.isnan(value):
            out[dates[i]] = value
    return out


def trailing_drop_map(dates: list[str], closes: list[float], lookback: int = LOOKBACK_DAYS) -> dict[str, float]:
    """交易日 -> 近 lookback 日累计涨跌幅，用于构造纯跌幅对照组。"""
    out: dict[str, float] = {}
    for i in range(lookback, len(dates)):
        base = float(closes[i - lookback])
        if base > 0:
            out[dates[i]] = (float(closes[i]) / base - 1.0) * 100.0
    return out


def _random_control(
    pool: list[str], forward: dict[str, float], size: int, baseline: float
) -> tuple[float | None, float | None, float | None, float]:
    """返回 (ci_low, ci_high, p_value, 随机均值差的中位数)。"""
    if size <= 0 or len(pool) <= size:
        return (None, None, None, 0.0)
    diffs: list[float] = []
    for seed in range(RANDOM_ROUNDS):
        rng = random.Random(seed)
        picked = rng.sample(pool, size)
        diffs.append(mean(forward[day] for day in picked) - baseline)
    diffs.sort()
    lo = diffs[int(0.025 * (len(diffs) - 1))]
    hi = diffs[int(0.975 * (len(diffs) - 1))]
    return (round(lo, 4), round(hi, 4), None, diffs[len(diffs) // 2])


def _p_value(pool: list[str], forward: dict[str, float], size: int, baseline: float, observed: float) -> float | None:
    if size <= 0 or len(pool) <= size:
        return None
    # 按观测方向取单尾：正超额问「随机能否更高」，负超额问「随机能否更低」。
    # 固定用 >= 会让负向结果得到 p≈0.99 这种反直觉的数，看起来像「极不显著」。
    hits = 0
    for seed in range(RANDOM_ROUNDS):
        rng = random.Random(seed)
        picked = rng.sample(pool, size)
        diff = mean(forward[day] for day in picked) - baseline
        if (diff >= observed) if observed >= 0 else (diff <= observed):
            hits += 1
    return round(hits / RANDOM_ROUNDS, 4)


def _verdict(stat_days: int, excess: float | None, ci: tuple[float | None, float | None], p: float | None) -> str:
    if stat_days < MIN_REGIME_DAYS or excess is None:
        return "样本不足"
    if ci[0] is not None and ci[0] <= excess <= ci[1]:
        return "落在随机区间内：无方向性"
    if p is not None and p <= 0.05:
        return "正向、超出随机（该状态后市场偏涨）" if excess > 0 else "负向、超出随机（该状态后市场偏跌）"
    return "超出随机区间但 p 不足：方向待更多样本"


def evaluate_regimes(
    regime_by_date: dict[str, str],
    dates: list[str],
    closes: list[float],
    *,
    horizon: int = 5,
    index_code: str = "000001",
) -> RegimeReport:
    forward = forward_return_map(dates, closes, horizon)
    pool = [day for day in dates if day in forward]
    baseline = mean(forward[day] for day in pool) if pool else None
    report = RegimeReport(
        horizon=horizon,
        index_code=index_code,
        window=(dates[0] if dates else "", dates[-1] if dates else ""),
        baseline=None if baseline is None else round(baseline, 4),
        baseline_days=len(pool),
    )
    if baseline is None:
        return report

    for regime in sorted({str(v) for v in regime_by_date.values() if v}):
        days = [day for day, value in regime_by_date.items() if str(value) == regime and day in forward]
        if not days:
            report.stats.append(RegimeStat(regime, 0, None, None, None, note="无可用前瞻样本"))
            continue
        value = mean(forward[day] for day in days)
        excess = value - baseline
        ci = (None, None)
        p = None
        if len(days) >= MIN_REGIME_DAYS:
            lo, hi, _, _ = _random_control(pool, forward, len(days), baseline)
            ci = (lo, hi)
            p = _p_value(pool, forward, len(days), baseline, excess)
        report.stats.append(
            RegimeStat(
                regime=regime,
                days=len(days),
                forward=round(value, 4),
                excess=round(excess, 4),
                positive_pct=round(100.0 * sum(1 for d in days if forward[d] > 0) / len(days), 2),
                p_value=p,
                random_ci=ci,
                verdict=_verdict(len(days), excess, ci, p),
            )
        )
    report.stats.sort(key=lambda s: (s.excess is None, -(s.excess or 0.0)))
    report.drawdown_control = _drawdown_control(regime_by_date, dates, closes, forward, pool, baseline)
    return report


def _drawdown_control(
    regime_by_date: dict[str, str],
    dates: list[str],
    closes: list[float],
    forward: dict[str, float],
    pool: list[str],
    baseline: float,
) -> dict[str, Any]:
    """CRASH 是否只是「跌多了」的同义词。"""
    crash_days = [d for d, v in regime_by_date.items() if str(v) == "CRASH" and d in forward]
    if len(crash_days) < MIN_REGIME_DAYS:
        return {"verdict": "样本不足", "crash_days": len(crash_days)}
    drops = trailing_drop_map(dates, closes)
    candidates = sorted((d for d in pool if d in drops), key=lambda d: drops[d])[: len(crash_days)]
    if not candidates:
        return {"verdict": "无对照样本"}
    crash_ret = mean(forward[d] for d in crash_days)
    control_ret = mean(forward[d] for d in candidates)
    overlap = len(set(crash_days) & set(candidates))
    increment = crash_ret - control_ret
    return {
        "crash_days": len(crash_days),
        "crash_forward": round(crash_ret, 4),
        "pure_drawdown_forward": round(control_ret, 4),
        "baseline_forward": round(baseline, 4),
        "increment_over_drawdown": round(increment, 4),
        "overlap_days": overlap,
        "verdict": (
            "CRASH 含独立于跌幅的信息"
            if increment > 0.3 and overlap <= len(crash_days) * 0.7
            else "与纯跌幅规则接近：可能只是短期均值回复"
        ),
    }
