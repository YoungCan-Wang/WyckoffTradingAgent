"""风格择时体检：池子自己的近期强弱,能不能预测池子接下来的表现。

起因:2026-09-05 把「选票」这条线量到底,两层都是噪声——

1. **池内排序无增量。** 推荐相对同规模池内随机子集,6 种灵活离场阈值(+3/-3/10日 …
   +5/-8/15日)**6/6 全落在随机带内**。且没有一档到 50%,区间 33.1%~40.6%,
   其中最高的 40.6% 来自 +5/-8 这种松止损——机械地用「亏更多」换「赢更频」,不是本事。
2. **入池相对市场按月翻号。** 卡钳匹配后 05 +19.2 / 06 +17.5 / 07 -5.7 / 08 -5.4 / 09 +2.1,
   月级 t=+1.03、正号 3/5。月内 |t| 高达 7 是因为同一批约 150 只票每天重抽,天与天不独立。

选票不动的话,剩下能动的只有择时。于是把三个候选放进同一套对照,**只有一个活下来**:

    池子近 5 日强弱     收益口径过全部对照;胜率口径没过月内置换   ← 活(仅收益口径)
    池子近 20 日强弱    全样本看着更强,剔掉 7 月只剩零头          ← 死
    水温 benchmark_regime  环移对照后全部落回区间内                ← 死

**一、活下来的那条,是风格择时而不是市场择时。** 按池子当日成分近 5 日已实现涨跌切三档
(69/64/73 天,取决于口径需要多少前瞻天):

    T+5 收益     弱档 -5.04%  强档 -0.58%  差 +4.46pct  相位 t=+4.82(5/5) 置换 p=0.025
    T+10 收益    弱档 -10.38% 强档 -0.86%  差 +9.52pct  相位 t=+2.50(7/10) 置换 p=0.001
    先触碰胜率   弱档 36.3%   强档 42.9%   差 +6.62pct  相位 t=+2.04(11/15) 置换 p=0.281

两道市场对照都没跟上:同一条规则套在**非池子市场股票**上(信号侧),T+5 只剩 +0.68pct;
按**池子**强弱切档去量**市场**的前瞻(前瞻侧),方向还相反(-1.83pct)。
所以量到的不是"大盘什么时候好"。留一月 4/4 不翻号、逐月独立分位 3/3 同向。

**二、但胜率口径没过月内置换,而胜率是第一优先级。** p=0.281 落在带内——它的价差
基本是月份效应,不是"强弱"本身在说话(逐月 2/3,7 月为负)。收益口径过(p=0.025 / 0.001)。
所以现在能说的是"强弱能预测**亏多少**",还不能说"能预测**赢不赢**"。

**三、最好的情况也只是少亏,不是赚。** 强档 T+10 是 **-0.86%**、胜率 **42.9%**,
距 50% 还差 7.1pct;同区间基准 -5.58%。这条信号把 -10.38% 挪到 -0.86%,没把负变正。
所以它的用法是"什么时候干脆别在里面",不是"什么时候加仓"。

**上表来自一次性脚本的数据复现。**首轮跑通 ``scripts/evaluate_style_timing.py``
(取 ``signal_outcomes`` 全池 1972 只、评估区间 2026-05-25~2026-08-28、69 天 4 个月)
得 T+5 弱档 -5.12% / 强档 -0.53% / 差 **+4.58pct**、相位 t=+5.40(5/5)、置换 p=0.027,
方向与量级一致。两边池子成分口径不完全相同,以驱动脚本的产物为准;每月重跑会覆盖这些数,
**引用前先看 JSON 里的 eval_window**。另注 ``min_retention`` 只有 0.385,
刚过 0.35 的门槛——这条结论在"剔掉某一个月"这个轴上并不宽裕。

**注:前瞻窗口不做截断。** 早先的一次性脚本用 ``j = min(i + h, len(s) - 1)`` 取前瞻,
尾部几天实际只持有了不到 h 天却仍记作 T+h,把 73 天凑齐的同时混进了短窗口。
这里改成"凑不齐 h 天就丢掉这天",于是 T+5 得 69 天、T+10 得 64 天,
数也随之变了(T+5 +4.64→+4.46,T+10 +6.59→+9.52)。宁可少几天,不要混口径。

**四、水温标签对池子没有方向性,先前看着有是月份混淆。** 随机抽日时 NEUTRAL 偏差
(-11.01% vs 基准 -5.32%)、BEAR_REBOUND 偏好(+0.39%),都"超出随机"。但水温是成片
出现的,随机抽日打散了连片结构、低估方差。换**环移对照**(整条标签序列相对日期平移,
run 长度与每档开启率精确保留)后:NEUTRAL p=0.074、BEAR_REBOUND p=0.132、
RISK_OFF p=0.412、CRASH p=0.294,**全部落回区间内**。真因是 NEUTRAL 的 13 天里 8 天
压在 7 月、BEAR_REBOUND 的 10 天里 9 天压在 8 月。

注意这与 ``regime_forward_eval`` 不冲突:那里量的是**指数**前瞻(CRASH 标底部,方向被
生产用反了),这里量的是**池子**前瞻。水温能说市场,不能说这个风格。

**五、回看 20 日是「避开 2026-07」的伪装,而三点网格上的形状本身就该起疑。**
全样本比 5 日更"显著",但留一月剔掉 7 月后只剩零头:``min_retention`` 只有 0.12,
即剔掉某一个月后幅度只剩全样本的 12%。逐月是 -2.01 / **-21.25** / -1.75——
一个月撑起全部。而且 5/10/20 日三档上**两端反号、中间无信息**,
这是小网格上读噪声的典型形状。先看形状,再看 p。

**判定链的顺序是有意的。** 样本不足 → 单月撑起 → 月内置换 → 市场对照 → 相位 t。
把 t 放最后,是因为 t 最容易被非独立样本抬起来:测试里那个纯噪声用例相位 |t| 高达 5.8,
只有月内置换挡得住它。所以"|t| 很大"从来不是放行理由。

**置换的 outside 与 p_value 必须同源。** 首轮真实数据上出过一次:``outside`` 当时按
"有符号 2.5/97.5 分位带"判、``p_value`` 按"双侧绝对值"算,零分布一偏两者就打架——
报告里于是出现「置换 p=0.03」配「落在带内,无信息」,而判定读的是 ``outside``,
把唯一活着的那档误杀了。现在 ``outside`` 直接由 ``p_value < 0.05`` 定义,
``band`` 只作展示(看零分布宽度仍有用)。同一个量有两套算法,迟早会在边界上不一致。

所以本轮**没有改任何生产开关**:不到 4 个可用月,不够动闸门。这个体检固化下来,
是为了让"下月重跑是否仍然成立"这件事有产物可比——而不是每次重新拍一遍脑袋。
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

ROUND_TRIP_COST_PCT = 0.202

# 一档至少这么多天才给判定，否则明确返回样本不足。
# 「不足 20 天不下判定」是尚未可知，不是对照未通过——两者不能混。
MIN_DAYS = 9
# 逐月独立分位要求月内至少这么多天（三等分后每档 >= 3）。
MIN_MONTH_DAYS = 9
# 一天至少这么多只票才算这天可用，避免单票噪声进日均。
MIN_NAMES_PER_DAY = 3
# 市场对照每天至少这么多只，口径比池子严：它是用来否定的，不能自己先不稳。
MIN_MARKET_NAMES = 30
TRAIL_WINDOWS = (5, 10, 20)


def tstat(values: list[float]) -> float | None:
    """手工单样本 t。环境无 scipy。"""
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(clean) < 3:
        return None
    avg = sum(clean) / len(clean)
    var = sum((v - avg) ** 2 for v in clean) / (len(clean) - 1)
    if var <= 0:
        return None
    return avg / math.sqrt(var / len(clean))


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass
class SpreadStat:
    """一次「强档减弱档」的估计。"""

    label: str
    days: int
    weak: float | None
    strong: float | None
    spread: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "days": self.days,
            "weak": _round(self.weak),
            "strong": _round(self.strong),
            "spread": _round(self.spread),
        }


@dataclass
class DayRow:
    """一个交易日的池子聚合观测。

    trail 是**当日回看**的已实现涨跌(不含未来),forward 是当日进、持有 H 日的前瞻。
    两者都按当日池子成分逐只算再取均值——不是拿一条池子指数,因为成分每天在换。
    """

    date: str
    pool_trail: dict[int, float | None]
    pool_forward: float | None
    pool_win: float | None
    n_pool: int
    market_trail: dict[int, float | None] = field(default_factory=dict)
    market_forward: float | None = None
    market_win: float | None = None
    n_market: int = 0

    @property
    def month(self) -> str:
        return self.date[:7]


def _terciles(pairs: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """按 key 升序三等分,返回(弱档值, 强档值)。中间档丢弃——只比两端。"""
    ordered = sorted(pairs, key=lambda kv: kv[0])
    cut = len(ordered) // 3
    if cut < 1:
        return [], []
    return [v for _, v in ordered[:cut]], [v for _, v in ordered[-cut:]]


def _spread(pairs: list[tuple[float, float]], label: str) -> SpreadStat:
    weak, strong = _terciles(pairs)
    if not weak or not strong:
        return SpreadStat(label=label, days=len(pairs), weak=None, strong=None, spread=None)
    w, s = mean(weak), mean(strong)
    return SpreadStat(label=label, days=len(pairs), weak=w, strong=s, spread=s - w)


def _pairs(
    rows: list[DayRow],
    window: int,
    value: str,
    *,
    key_side: str = "pool",
    value_side: str = "pool",
) -> list[tuple[float, float]]:
    """取出(切档键, 被测值)。任一为空就整天丢掉,不做插补。

    key_side / value_side 可以分别取 pool 或 market,四种组合各答一个问题:

        pool  → pool    主结果:池子强弱能不能预测池子
        market→ market  信号侧对照:同一条规则套在市场上还成不成立
        pool  → market  前瞻侧对照:池子强的日子,整个市场是不是也好
        market→ pool    (未用)
    """
    out: list[tuple[float, float]] = []
    for row in rows:
        if key_side == "market":
            key = row.market_trail.get(window)
        else:
            key = row.pool_trail.get(window)
        if value_side == "market":
            val = row.market_forward if value == "forward" else row.market_win
        else:
            val = row.pool_forward if value == "forward" else row.pool_win
        if "market" in (key_side, value_side) and row.n_market < MIN_MARKET_NAMES:
            continue
        if "pool" in (key_side, value_side) and row.n_pool < MIN_NAMES_PER_DAY:
            continue
        if key is None or val is None:
            continue
        out.append((float(key), float(val)))
    return out


def _phase_scan(rows: list[DayRow], window: int, value: str, stride: int) -> dict[str, Any]:
    """每 stride 天取一天,扫遍所有相位。

    相邻交易日共用大部分前瞻窗口,天与天不独立,直接用全样本会把显著性抬高。
    只报一个相位就是挑选,所以扫全并给相位间的 t。

    stride 必须取**被测值自己的窗口长度**:前瞻收益取 horizon,先触碰胜率取
    max_hold(它最长能拖到那么久)。取小了窗口仍然重叠,独立性是假的。
    """
    spreads: list[float] = []
    for phase in range(max(1, stride)):
        sub = rows[phase :: max(1, stride)]
        stat = _spread(_pairs(sub, window, value), f"phase{phase}")
        if stat.spread is not None:
            spreads.append(stat.spread)
    if not spreads:
        return {"phases": 0, "mean": None, "t": None, "positive": None}
    return {
        "phases": len(spreads),
        "spreads": [_round(v) for v in spreads],
        "mean": _round(mean(spreads)),
        "t": _round(tstat(spreads), 3),
        "positive": f"{sum(1 for v in spreads if v > 0)}/{len(spreads)}",
    }


def _leave_one_month_out(rows: list[DayRow], window: int, value: str) -> dict[str, Any]:
    """逐月剔除后重估。一个月撑起全部的结论,剔掉它就会塌——20 日回看就是这么死的。"""
    months = sorted({r.month for r in rows})
    if len(months) < 3:
        return {"months": len(months), "verdict": "月份不足,不做留一月"}
    out: dict[str, float | None] = {}
    for drop in months:
        stat = _spread(_pairs([r for r in rows if r.month != drop], window, value), f"drop_{drop}")
        out[drop] = _round(stat.spread)
    kept = [v for v in out.values() if v is not None]
    full = _spread(_pairs(rows, window, value), "full").spread
    # 符号一致性要对齐全样本方向：全样本为负时,「全部为负」才叫没翻号。
    ref = 1.0 if (full or 0.0) >= 0 else -1.0
    return {
        "months": len(months),
        "without": out,
        "positive": f"{sum(1 for v in kept if v > 0)}/{len(kept)}" if kept else None,
        "same_sign": f"{sum(1 for v in kept if v * ref > 0)}/{len(kept)}" if kept else None,
        # 剔掉任一月后剩余幅度的最小绝对值 / 全样本幅度。接近 0 = 单月撑起全部。
        "min_retention": _round(min(abs(v) for v in kept) / abs(full) if kept and full else None, 3),
    }


def _within_month(rows: list[DayRow], window: int, value: str) -> dict[str, Any]:
    """月内独立切档。跨月切档会把「哪个月好」读成「哪档好」。"""
    bym: dict[str, list[DayRow]] = {}
    for row in rows:
        bym.setdefault(row.month, []).append(row)
    per: dict[str, float | None] = {}
    for month, days in sorted(bym.items()):
        if len(days) < MIN_MONTH_DAYS:
            continue
        stat = _spread(_pairs(days, window, value), month)
        if stat.spread is not None:
            per[month] = _round(stat.spread)
    vals = [v for v in per.values() if v is not None]
    return {
        "per_month": per,
        "mean": _round(mean(vals)) if vals else None,
        "positive": f"{sum(1 for v in vals if v > 0)}/{len(vals)}" if vals else None,
    }


def _month_block_permutation(rows: list[DayRow], window: int, value: str, draws: int = 2000) -> dict[str, Any]:
    """月内打乱切档键。

    保留每天归属哪个月、也保留被测值本身,只把「这天算强还是算弱」在月内重排。
    这样随机带里已经含了月份效应,超出带才算池子强弱本身在说话。
    """
    pairs_by_month: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        if row.n_pool < MIN_NAMES_PER_DAY:
            continue
        key = row.pool_trail.get(window)
        val = row.pool_forward if value == "forward" else row.pool_win
        if key is None or val is None:
            continue
        pairs_by_month.setdefault(row.month, []).append((float(key), float(val)))
    flat = [kv for lst in pairs_by_month.values() for kv in lst]
    observed = _spread(flat, "observed").spread
    if observed is None or len(flat) < MIN_DAYS:
        return {"draws": 0, "observed": _round(observed), "verdict": "样本不足,不做置换"}
    null: list[float] = []
    for seed in range(draws):
        rng = random.Random(seed)
        shuffled: list[tuple[float, float]] = []
        for lst in pairs_by_month.values():
            keys = [k for k, _ in lst]
            rng.shuffle(keys)
            shuffled.extend((k, v) for k, (_, v) in zip(keys, lst))
        got = _spread(shuffled, "null").spread
        if got is not None:
            null.append(got)
    if not null:
        return {"draws": 0, "observed": _round(observed), "verdict": "置换未产出"}
    ordered = sorted(null)
    lo = ordered[int(0.025 * len(ordered))]
    hi = ordered[min(len(ordered) - 1, int(0.975 * len(ordered)))]
    tail = sum(1 for v in null if abs(v) >= abs(observed))
    p_value = (tail + 1) / (len(null) + 1)
    # outside 必须与 p_value 同源。早先用「有符号 2.5/97.5 分位带」判 outside、
    # 却用「双侧绝对值」算 p,零分布一偏两者就会打架:实测 +4.58 落在带内、
    # 而 p=0.03 —— 判定读的是 outside,于是报告里出现「p=0.03」配「带内,无信息」。
    # 展示用的 band 保留(看零分布宽度有用),但不再参与判定。
    return {
        "draws": len(null),
        "observed": _round(observed),
        "band": [_round(lo), _round(hi)],
        "p_value": _round(p_value, 4),
        "outside": bool(p_value < 0.05),
    }


def _verdict(block: dict[str, Any]) -> str:
    """判定。四种结果里「样本不足」是尚未可知,不能写成对照未通过。"""
    full = block["full_sample"]["spread"]
    if block["full_sample"]["days"] < MIN_DAYS or full is None:
        return "样本不足,不下判定"

    phase_t = block["phase_scan"].get("t")
    lomo = block["leave_one_month_out"]
    retention = lomo.get("min_retention")
    if retention is not None and retention < 0.35:
        return "单月撑起全部,不成立"

    perm = block["month_block_permutation"]
    if perm.get("outside") is False:
        return "落在月内置换带内,无信息"

    # 两道市场对照任一给出同量级同方向,就说明量到的是市场择时,不是风格择时。
    for name in ("market_signal_control", "market_forward_control"):
        got = block[name]["spread"]
        if got is not None and full != 0 and got * full > 0 and abs(got) >= 0.5 * abs(full):
            return f"{block[name]['label']}给出同量级,是市场择时不是风格择时"

    if phase_t is None:
        return "相位不足,证据不完整"
    # 留一月要求每一次剔除后符号都不翻——翻了就说明结论依赖特定月份。
    got, total = ((lomo.get("same_sign") or "0/0").split("/") + ["0"])[:2]
    signs_ok = total not in ("0", "") and got == total
    if abs(phase_t) >= 2.5 and signs_ok:
        return "过全部对照"
    return "方向一致但强度不足,继续累样本"


@dataclass
class StyleTimingReport:
    horizon: int
    value_kind: str
    days: int
    months: list[str]
    eval_window: list[str]
    baseline: float | None
    stride: int = 0
    windows: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def evaluate_style_timing(
    rows: list[DayRow], horizon: int, value_kind: str = "forward", stride: int | None = None
) -> StyleTimingReport:
    """把一批日观测跑成一份体检。value_kind: forward=前瞻收益, win=先触碰胜率。

    stride 是不重叠抽样的步长,不传则按 horizon。胜率口径必须显式传 max_hold——
    先触碰能拖到 max_hold 天,用 horizon 当步长窗口仍然重叠。
    """
    usable = [
        r
        for r in rows
        if r.n_pool >= MIN_NAMES_PER_DAY and (r.pool_forward if value_kind == "forward" else r.pool_win) is not None
    ]
    usable.sort(key=lambda r: r.date)
    base_vals = [
        float(r.pool_forward if value_kind == "forward" else r.pool_win)  # type: ignore[arg-type]
        for r in usable
    ]
    report = StyleTimingReport(
        horizon=horizon,
        value_kind=value_kind,
        days=len(usable),
        months=sorted({r.month for r in usable}),
        # 只信产物里的 eval_window：预热吃掉的天数会让 --start 骗人。
        eval_window=[usable[0].date, usable[-1].date] if usable else [],
        baseline=_round(mean(base_vals)) if base_vals else None,
    )
    step = stride if stride and stride > 0 else horizon
    report.stride = step
    for window in TRAIL_WINDOWS:
        block: dict[str, Any] = {
            "full_sample": _spread(_pairs(usable, window, value_kind), f"trail{window}").as_dict(),
            "phase_scan": _phase_scan(usable, window, value_kind, step),
            "leave_one_month_out": _leave_one_month_out(usable, window, value_kind),
            "within_month": _within_month(usable, window, value_kind),
            "month_block_permutation": _month_block_permutation(usable, window, value_kind),
            # 同一条规则套在市场上还成不成立。成立就说明这不是风格特有的。
            "market_signal_control": _spread(
                _pairs(usable, window, value_kind, key_side="market", value_side="market"),
                "信号侧市场对照",
            ).as_dict(),
            # 池子强的日子,整个市场是不是也好。也好就说明量到的是大盘。
            "market_forward_control": _spread(
                _pairs(usable, window, value_kind, key_side="pool", value_side="market"),
                "前瞻侧市场对照",
            ).as_dict(),
        }
        block["verdict"] = _verdict(block)
        report.windows[f"trail{window}"] = block

    if report.days < 100:
        report.notes.append(f"样本 {report.days} 天 / {len(report.months)} 个月,不足以改任何生产开关;本轮只做累积。")
    strong = report.windows.get("trail5", {}).get("full_sample", {}).get("strong")
    if strong is not None and value_kind == "forward" and strong < 0:
        report.notes.append(
            f"强档绝对水平 {strong:+.2f}% 仍为负——这条信号是「什么时候别在里面」,不是「什么时候加仓」。"
        )
    return report


def report_to_dict(report: StyleTimingReport) -> dict[str, Any]:
    return asdict(report)
