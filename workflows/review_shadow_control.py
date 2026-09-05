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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.funnel_effect_eval import (
    CONTROL_SEEDS,
    MIN_DAYS,
    MIN_HITS_PER_DAY,
    control_gap,
    evaluate_daily,
    summarize_absolute,
    summarize_group,
    win_control_gap,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免运行时强依赖
    from core.funnel_effect_eval import Panels
    from workflows.review_shadow_backtest import ShadowTrade

# 对照评估的持有期（交易日）。与影子回测的 ret_t1/t3/t5 对齐，
# 便于把「裸收益」和「同动量超额」逐格对照，而不是各报一套窗口。
CONTROL_HORIZONS = (1, 3, 5)


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
