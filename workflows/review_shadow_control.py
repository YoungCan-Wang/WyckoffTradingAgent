"""影子车道的同动量对照：把裸收益换成「相对同动量同侪的超额」。

为什么必须有这一节
------------------
``_lane_summary`` 报的是裸 T+1/T+3/T+5 均值与胜率。影子车道天生偏高动量（near_l2
差一点就过结构强度、pre_breakout 按 watch_score 排序），裸收益里混着动量的 beta。
2026-06~08 那段样本上全市场大跌，只看裸收益会把「跟跌少一点」读成选股能力——
full-market-control-confounds-momentum 记的就是这个坑：候选动量 +19% vs 全市场
-5%，不做最近邻匹配就会把择时读成选股。

所以「爆发前夜要不要补强」这个问题，只有配对超额跑赢**随机负控制**才算是"要"。
统计全部复用 ``core.funnel_effect_eval``（``match_by_momentum`` / ``sample_momentum_band``
/ ``control_gap``），这里只做形状适配，不新写一套统计口径。

四栏必须同时读
--------------
- ``absolute``：拿着这批票赚不赚钱（分母=全部车道票），含**股级**胜率。
- ``matched``：同动量同侪里选得好不好（分母=配对成功的子集，与对照组一致）。
- ``control_gap``：收益超额是不是只来自动量选位。
- ``win_control_gap``：胜率超额是不是只来自动量选位。胜率必须自己过一遍控制，
  不能借收益那一栏的结论——风格择时那轮实测两栏分家：收益过了月内置换
  （T+5 p=0.025），胜率没过（p=0.284）。

反向臂：先分开「无效」和「符号反了」
------------------------------------
上面四栏回答的是「这条车道含不含选股信息」。它答不了另一个问题：**排序键的符号
对不对**。周末 eval 量到 ``factor_ic_eval`` 50 行里 16 行判可用、其中 15 行为反向
（docs/ITERATION_STRATEGY.md 2026-09-13），而「信号无效」与「信号符号反了」指向完全
相反的动作——一个是删掉这一项，一个是把它乘 -1。两种解释在裸收益和配对超额上都长
得一样（都是负超额），只有把同一条车道按排序键切成两半、比两半各自的同动量超额，
才分得开。这就是 ``evaluate_score_direction``。
"""

from __future__ import annotations

from statistics import mean
from typing import TYPE_CHECKING, Any

from core.funnel_effect_eval import (
    CONTROL_SEEDS,
    MIN_DAYS,
    MIN_HITS_PER_DAY,
    control_gap,
    evaluate_daily,
    summarize_absolute,
    summarize_group,
    tstat,
    win_control_gap,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免运行时强依赖
    from core.funnel_effect_eval import Panels
    from workflows.review_shadow_backtest import ShadowTrade

# 对照评估的持有期（交易日）。与影子回测的 ret_t1/t3/t5 对齐，
# 便于把「裸收益」和「同动量超额」逐格对照，而不是各报一套窗口。
CONTROL_HORIZONS = (1, 3, 5)

# 反向臂的 t 门槛。与仓里其它走前判据（``diff_t>=2.0``）同一个数：1.96 是双侧 5% 的
# 临界点，2.0 取整后仍在其上。1.80<=|t|<2.0 明确算没过，不四舍五入 —— 方向判定要拿来
# 动排序权重，贴线放行等于把一次抽样噪声写进生产。
DIRECTION_T_GATE = 2.0


def lane_day_map(trades: list[ShadowTrade]) -> dict[str, dict[str, Any]]:
    """车道票按信号日分组，塞进 resolve_layer 认的形状。

    ``formal_l4`` 与 ``all`` 都填同一批车道票：对 ``status="formal_l4"``，
    ``resolve_layer`` 取 ``hits=formal_l4``、``pool=universe-all``，正好是
    「这条车道 vs 同日流动性池里的其它票」。
    """
    by_day: dict[str, set[str]] = {}
    for trade in trades:
        by_day.setdefault(str(trade.signal_date), set()).add(str(trade.code))
    return {ds: {"formal_l4": sorted(codes), "all": sorted(codes)} for ds, codes in by_day.items()}


def evaluate_lane_control(
    trades: list[ShadowTrade],
    panels: Panels,
    horizons: tuple[int, ...] = CONTROL_HORIZONS,
) -> dict[str, Any]:
    """单条车道的绝对收益 + 配对超额 + 随机负控制，按持有期分列。"""
    cands = lane_day_map(trades)
    if len(cands) < MIN_DAYS:
        return {
            "eligible": False,
            "reason": f"交易日仅 {len(cands)} 天，不足 {MIN_DAYS} 天，不下判定",
            "signal_days": len(cands),
        }
    result: dict[str, Any] = {"eligible": True, "signal_days": len(cands), "horizons": {}}
    for horizon in horizons:
        rows = evaluate_daily(cands, panels, horizon, status="formal_l4")
        matched = summarize_group("matched", rows["matched"])
        controls = [summarize_group(f"control_{seed}", rows[f"control_{seed}"]) for seed in CONTROL_SEEDS]
        result["horizons"][str(horizon)] = {
            "absolute": summarize_absolute(rows["absolute"]).as_dict(),
            "matched": matched.as_dict(),
            "controls": [c.as_dict() for c in controls],
            "control_gap": control_gap(matched, controls),
            # 车道比较同样要看胜率:两条车道的收益差与胜率差实测会分家。
            "win_control_gap": win_control_gap(matched, controls),
        }
    return result


def lane_control_summary(
    trades: list[ShadowTrade],
    panels: Panels | None,
    horizons: tuple[int, ...] = CONTROL_HORIZONS,
) -> dict[str, Any]:
    """全部车道的对照结果。缺面板时显式降级，不能让读者以为「没这一节」等于「过了」。"""
    if panels is None:
        return {
            "available": False,
            "reason": "快照缺 hist_full.csv.gz，无法构建同动量对照；本次只有裸收益，不可据此判定选股能力",
        }
    lanes = sorted({trade.lane for trade in trades})
    return {
        "available": True,
        "note": (
            f"对照池=同日流动性池内非本车道票，按 T 日已知 20 日涨幅 1:1 无放回最近邻配对；"
            f"随机负控制 {len(CONTROL_SEEDS)} 个种子。持有期重叠，所有 t 值都被高估。"
            f"每日最少 {MIN_HITS_PER_DAY} 只、最少 {MIN_DAYS} 个交易日才下判定。"
        ),
        "lanes": {
            lane: evaluate_lane_control([t for t in trades if t.lane == lane], panels, horizons) for lane in lanes
        },
    }


def _ranked_by_day(trades: list[ShadowTrade]) -> dict[str, list[ShadowTrade]]:
    """按信号日收拢**带排序分**的票。没有分就没有「高半 / 低半」可言，直接剔掉。

    同日同代码去重：一天里同一只票出现两次会让它同时进两半，差值两头都被它带偏。
    """
    by_day: dict[str, dict[str, ShadowTrade]] = {}
    for trade in trades:
        if not trade.ranked or trade.score is None:
            continue
        by_day.setdefault(str(trade.signal_date), {}).setdefault(str(trade.code), trade)
    return {ds: list(rows.values()) for ds, rows in by_day.items()}


def score_split_day_maps(trades: list[ShadowTrade]) -> tuple[dict[str, Any], dict[str, Any], int]:
    """把车道票按影子分切成高分半 / 低分半，各自塞成 ``resolve_layer`` 认的形状。

    两份的 ``all`` 都填**当日整条车道**的票，不是各自那一半。``resolve_layer`` 取
    ``pool = universe - all``：``all`` 只填自己那半的话，另一半就落进对方的对照池——
    高半的随机篮里混着低半的票、低半的篮里混着高半的，两端的超额被同一批票同时压低
    和抬高，差值读出来的不再是排序键的方向。两半共用同一个非车道池才可比。

    奇数只时中位那只两半都不进：它落哪边纯看并列打破方式，而两半只数相等才让噪声量级
    对齐（``MIN_HITS_PER_DAY`` 也是按每半各自算的）。
    """
    high: dict[str, Any] = {}
    low: dict[str, Any] = {}
    for ds, rows in _ranked_by_day(trades).items():
        rows.sort(key=lambda t: float(t.score), reverse=True)
        cut = len(rows) // 2
        if cut < MIN_HITS_PER_DAY:
            continue
        lane_codes = sorted(str(t.code) for t in rows)
        high[ds] = {"formal_l4": sorted(str(t.code) for t in rows[:cut]), "all": lane_codes}
        low[ds] = {"formal_l4": sorted(str(t.code) for t in rows[-cut:]), "all": lane_codes}
    return high, low, len(high)


def evaluate_score_direction(
    trades: list[ShadowTrade],
    panels: Panels,
    horizons: tuple[int, ...] = CONTROL_HORIZONS,
) -> dict[str, Any]:
    """单条车道的排序键方向：高分半 vs 低分半的同动量超额差，按持有期分列。

    每半各自减掉自己的配对基准，所以差值里已经不含两半各自的动量水平——问的是
    「高半跑赢它的同动量同侪，比低半跑赢它的同侪多多少」。
    """
    high_map, low_map, split_days = score_split_day_maps(trades)
    if not any(t.ranked and t.score is not None for t in trades):
        # 「本层无排序键」和「样本还不够」是两回事，沿用 _score_band_summary 的措辞与键名：
        # 前者攒数据也不会变（rotation_setup 只作标签、老 trace 缺 watch_score），后者下个月
        # 就能重跑。混成一句「不足 N 天」会让人以为等下去就有方向判定。
        return {
            "eligible": False,
            "ranked": False,
            "reason": "本层无连续排序键，切不出高低半，方向判定不适用（攒样本也不会变）",
            "split_days": 0,
        }
    if split_days < MIN_DAYS:
        return {
            "eligible": False,
            "ranked": True,
            "reason": (
                f"可切分信号日仅 {split_days} 天，不足 {MIN_DAYS} 天，不下判定"
                f"（每天需 >= {MIN_HITS_PER_DAY * 2} 只带排序分的票才切得出两半）"
            ),
            "split_days": split_days,
        }
    result: dict[str, Any] = {"eligible": True, "ranked": True, "split_days": split_days, "horizons": {}}
    for horizon in horizons:
        result["horizons"][str(horizon)] = _direction_cell(
            evaluate_daily(high_map, panels, horizon, status="formal_l4"),
            evaluate_daily(low_map, panels, horizon, status="formal_l4"),
        )
    return result


def score_direction_summary(
    trades: list[ShadowTrade],
    panels: Panels | None,
    horizons: tuple[int, ...] = CONTROL_HORIZONS,
) -> dict[str, Any]:
    """全部车道的反向臂。缺面板时显式降级——这一节缺席不等于「方向没问题」。"""
    if panels is None:
        return {
            "available": False,
            "reason": "快照缺 hist_full.csv.gz，两半都没有同动量对照可减，反向臂无从算起",
        }
    lanes = sorted({trade.lane for trade in trades})
    return {
        "available": True,
        "note": (
            f"按信号日把车道内带排序分的票切成高分半 / 低分半（只数相等，奇数只时中位那只两半都不进），"
            f"两半共用同一个非车道对照池，各自减自己的最近邻配对基准后取逐日差。"
            f"判定门槛 |t| >= {DIRECTION_T_GATE}，且差值须超随机负控制自身的离散度。"
            f"低半显著更好 → 排序键符号反了；两半都负且差不显著 → 排序键无信息。"
        ),
        "t_gate": DIRECTION_T_GATE,
        "lanes": {
            lane: evaluate_score_direction([t for t in trades if t.lane == lane], panels, horizons) for lane in lanes
        },
    }


def _excess_by_day(rows: list[dict[str, Any]]) -> dict[str, float]:
    """逐日 ``net - control``。缺任一栏就丢掉那天，不按 0 补。

    按 0 补等于把「这天没算出对照」当成「对照恰好打平」，会凭空造出一个差值——
    ``summarize_group`` 在胜率那栏踩过同一个坑，这里沿用它的处理。
    """
    out: dict[str, float] = {}
    for row in rows:
        net = row.get("net")
        ctrl = row.get("control")
        if net is None or ctrl is None:
            continue
        out[str(row.get("date"))] = float(net) - float(ctrl)
    return out


def _paired_diffs(high: dict[str, Any], low: dict[str, Any], key: str) -> list[float]:
    """两半在**同一天**的超额差。只有两边都算出来的日子才进样本。"""
    hi = _excess_by_day(high.get(key) or [])
    lo = _excess_by_day(low.get(key) or [])
    return [hi[ds] - lo[ds] for ds in sorted(set(hi) & set(lo))]


def _direction_cell(high: dict[str, Any], low: dict[str, Any]) -> dict[str, Any]:
    """一个持有期的高半 − 低半，附随机负控制的同口径差。

    对照不是「零」：两半各自减掉配对基准后，**两个同动量随机篮之间照样有一个非零的
    差**，那就是这条判据的噪声地板。t 显著却落在地板宽度里，不算方向证据。
    """
    hi = _excess_by_day(high.get("matched") or [])
    lo = _excess_by_day(low.get("matched") or [])
    days = sorted(set(hi) & set(lo))
    if len(days) < MIN_DAYS:
        return {
            "days": len(days),
            "verdict": f"两半同日配对成功仅 {len(days)} 天，不足 {MIN_DAYS} 天，不下判定",
        }
    diffs = [hi[ds] - lo[ds] for ds in days]
    controls = [mean(d) for d in (_paired_diffs(high, low, f"control_{s}") for s in CONTROL_SEEDS) if d]
    high_excess = mean(hi[ds] for ds in days)
    low_excess = mean(lo[ds] for ds in days)
    diff, diff_t = mean(diffs), tstat(diffs)
    return {
        "days": len(days),
        "high_excess_pct": _r(high_excess),
        "low_excess_pct": _r(low_excess),
        "diff_pct": _r(diff),
        "diff_t": _r(diff_t),
        "control_seeds": len(controls),
        "control_diff_avg": _r(mean(controls)) if controls else None,
        "control_diff_min": _r(min(controls)) if controls else None,
        "control_diff_max": _r(max(controls)) if controls else None,
        "verdict": _direction_verdict(diff, diff_t, controls, high=high_excess, low=low_excess),
    }


def _direction_verdict(
    diff: float,
    diff_t: float | None,
    controls: list[float],
    *,
    high: float,
    low: float,
) -> str:
    """两个方向各自要过两道：|t| 到门槛，且差值超出随机负控制的离散度。

    判据抄 docs/ITERATION_STRATEGY.md 的 P0 行：低半显著更好 → 排序键符号反了；两半
    都负且差不显著 → 排序键无信息。中间那种（显著但没超地板）单列，不能并进「无信息」
    —— 那是「量到了但量不准」，样本长起来可能翻成结论，而「无信息」是要删掉这一项的。
    """
    if diff_t is None:
        return "逐日差方差为零或样本过少，算不出 t，不下判定"
    floor = max(abs(v) for v in controls) if controls else None
    if abs(diff_t) < DIRECTION_T_GATE:
        if high < 0 and low < 0:
            return f"两半都是负超额（高半 {high:+.3f} / 低半 {low:+.3f}pct）且差不显著（t={diff_t:+.2f}）：排序键无信息"
        return f"高低半差不显著（t={diff_t:+.2f}）：方向未定，不动排序权重"
    if floor is not None and abs(diff) <= floor:
        return f"差 {diff:+.3f}pct 显著（t={diff_t:+.2f}）但未超随机负控制离散度 {floor:.3f}pct：证据不足"
    if diff < 0:
        return f"低半显著更好（差 {diff:+.3f}pct，t={diff_t:+.2f}，已超随机负控制离散度）：排序键符号反了"
    return f"高半显著更好（差 {diff:+.3f}pct，t={diff_t:+.2f}，已超随机负控制离散度）：排序键方向正确"


def direction_verdict_lines(summary: dict[str, Any]) -> list[str]:
    """报告里的反向臂小节。它缺席时必须印出原因，否则读者会当成「方向没问题」。"""
    if not summary.get("available"):
        return ["## 排序键方向（反向臂）", "", f"未出反向臂：{summary.get('reason') or '未知原因'}", ""]
    lines = ["## 排序键方向（反向臂）", "", summary.get("note") or "", ""]
    for lane, block in (summary.get("lanes") or {}).items():
        if not block.get("eligible"):
            lines.append(f"- **{lane}**：{block.get('reason') or '样本不足'}")
            continue
        lines.append(f"- **{lane}**（{block.get('split_days')} 个可切分信号日）")
        for horizon, cell in (block.get("horizons") or {}).items():
            lines.append(f"  - T+{horizon} {_direction_line(cell)}")
    return [*lines, ""]


def _direction_line(cell: dict[str, Any]) -> str:
    """两半各自的超额也要印出来：只看差值分不出「都负但高半没那么负」和「一正一负」。"""
    if cell.get("diff_pct") is None:
        return f"{cell.get('verdict') or '样本不足'}"
    return (
        f"高半 {_num(cell.get('high_excess_pct'))}pct / 低半 {_num(cell.get('low_excess_pct'))}pct，"
        f"差 {_num(cell.get('diff_pct'))}pct（t={_num(cell.get('diff_t'))}，{cell.get('days')} 天，"
        f"随机负控制差 {_num(cell.get('control_diff_min'))}~{_num(cell.get('control_diff_max'))}）"
        f" → {cell.get('verdict') or '样本不足'}"
    )


def control_verdict_lines(summary: dict[str, Any]) -> list[str]:
    """报告里的对照小节。裸收益那张表旁边必须有这几行，否则动量选位会被读成选股。"""
    if not summary.get("available"):
        return ["## 同动量对照", "", f"未出对照：{summary.get('reason') or '未知原因'}", ""]
    lines = ["## 同动量对照", "", summary.get("note") or "", ""]
    for lane, block in (summary.get("lanes") or {}).items():
        if not block.get("eligible"):
            lines += [f"- **{lane}**：{block.get('reason') or '样本不足'}"]
            continue
        lines.append(f"- **{lane}**（{block.get('signal_days')} 个信号日）")
        for horizon, cell in (block.get("horizons") or {}).items():
            lines.append(f"  - T+{horizon} {_cell_line(cell)}")
    return [*lines, ""]


def _cell_line(cell: dict[str, Any]) -> str:
    """绝对、股级胜率、超额同时出：任何一栏单看都会得出相反结论。

    ``positive_day_pct`` 是**日级**的（当天这一篮均值为正的日子占比），不是胜率，
    早先这里就把它写成了「胜率」。一篮 3 只 +20% / 7 只 -5%，日级算赢、股级只有
    30%，两者能同时成立。真正回答「选出来的票赚不赚钱」的是 ``stock_win_pct``。
    """
    absolute = cell.get("absolute") or {}
    matched = cell.get("matched") or {}
    gap = cell.get("control_gap") or {}
    win_gap = cell.get("win_control_gap") or {}
    abs_txt = (
        f"绝对 {_num(absolute.get('net_pct'))}%"
        f"（股级胜率 {_num(absolute.get('stock_win_pct'))}%，"
        f"正收益日 {_num(absolute.get('positive_day_pct'))}%，{absolute.get('verdict')}）"
    )
    exc_txt = f"配对超额 {_num(matched.get('excess_pct'))}pct（t={_num(matched.get('excess_t'))}）→ {gap.get('verdict') or '样本不足'}"
    win_txt = (
        f"胜率超额 {_num(matched.get('stock_win_excess_pct'))}pct"
        f"（t={_num(matched.get('stock_win_excess_t'))}）→ {win_gap.get('verdict') or '样本不足'}"
    )
    return f"{abs_txt}；{exc_txt}；{win_txt}"


def _num(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f}"


def _r(value: float | None) -> float | None:
    """与 ``core.funnel_effect_eval._round`` 同一口径（4 位），让两节的数字能逐位对读。"""
    return None if value is None else round(float(value), 4)
