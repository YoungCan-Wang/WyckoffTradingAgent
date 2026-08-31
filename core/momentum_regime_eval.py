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
    """一种动态切换设计的走前结果。"""

    label: str
    days: int
    switched_ret: float | None
    baseline_ret: float | None
    diff: float | None
    diff_t: float | None
    on_rate: float | None
    on_rate_by_quarter: dict[int, float] = field(default_factory=dict)

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
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        if self.days < MIN_DAYS or self.diff is None or self.diff_t is None:
            return "样本不足"
        if self.diff_t < 2.0:
            return "走前不显著：不可上线"
        return "走前显著：值得进一步验证"


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class MomentumReport:
    thresholds: list[BandStat] = field(default_factory=list)
    mid_band: BandStat | None = None
    domain: BandStat | None = None
    switches: list[SwitchStat] = field(default_factory=list)
    ic_persistence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholds": [s.as_dict() for s in self.thresholds],
            "mid_band": None if self.mid_band is None else self.mid_band.as_dict(),
            "domain": None if self.domain is None else self.domain.as_dict(),
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
                "首轮两条都不成立。switches 的 diff_t 是走前口径，样本内回看不算。"
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


def walk_forward_switch(
    label: str,
    rows: list[dict[str, float]],
    *,
    state_key: str,
    warmup: int = 120,
    high_is_on: bool = True,
) -> SwitchStat:
    """走前切换：T 日只用 T 之前的历史定阈值，关闭时退到中动量档而非空仓。

    这是唯一能否掉「样本内回看显著」的环节。首轮四种设计全部在这里失效。
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
    return SwitchStat(
        label=label,
        days=len(switched),
        switched_ret=sum(switched) / len(switched),
        baseline_ret=sum(baseline) / len(baseline),
        diff=sum(diffs) / len(diffs),
        diff_t=tstat(diffs),
        on_rate=sum(on_flags) / len(on_flags),
        on_rate_by_quarter={q: sum(v) / len(v) for q, v in on_by_quarter.items()},
    )


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
        sum(1 for a, b in zip(signs, signs[1:], strict=False) if a == b) / (len(signs) - 1)
        if len(signs) > 1
        else None
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
    if report.domain is not None:
        lines.append(_band_row(report.domain, quarters))
    lines += ["", "| 动态切换设计 | 天数 | 切换后 | 固定放行 | 差值 | 差值t | 开启率 | 判定 |", "| --- | --: | --: | --: | --: | --: | --: | --- |"]
    for stat in report.switches:
        lines.append(
            f"| {stat.label} | {stat.days} | {_signed(stat.switched_ret)} | {_signed(stat.baseline_ret)} | "
            f"{_num(stat.diff)} | {_num(stat.diff_t, 2)} | {_pct(stat.on_rate)} | {stat.verdict} |"
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
    survived = [s.label for s in report.switches if s.diff_t is not None and s.diff_t >= 2.0]
    if survived:
        out.append(f"- ③ 这些切换设计走前显著：{'、'.join(survived)}——值得进一步验证再考虑上线。")
    else:
        out.append("- ③ 所有动态切换设计走前均不显著，维持固定放行。样本内回看显著不算数。")
    out.append("- ④ 任何参数改动落地前，须按本脚本 walk_forward_switch 的走前口径复验，且增益需大于成本 0.202%。")
    return out


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
