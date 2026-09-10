"""复盘漏掉的票在 T+h 上到底赚不赚钱：捕获表的前瞻收益评估。

``review_capture_daily`` 只存信号日状态（哪只票卡在哪道闸），不存前瞻收益——收益在
评估时用行情 join 出来，与 ``review_shadow_lane_daily`` 同一套做法。原因有两条：复盘
当天算不出 T+10（还差十个交易日），以及表里已经有一个 ``gain_pct``，再加一列「收益」
必然被混用（见 memory control-row-must-measure-itself 与
one-field-two-meanings-corrupts-column）。所以这里不加列、不发 DDL。

入场日有两种，必须分开报
------------------------
漏斗在 T-1 收盘后跑，给的是 T 日的买入建议。所以「这只票该不该抓」的真实入场价是
**T 日开盘**：``window(previous_trade_date, h)`` 正好买在那里。

但复盘池是按「T 日涨幅 >7%」挑出来的，T 日落在持有窗口里就意味着这批票按结果选样：
任何按 T-1 信息配对的对照必然输给它们，而输的原因正是它们被选中的原因。所以
``signal_day`` 这一档只出**绝对收益**，配对与随机负控制被显式**拒绝**——不是省略，
是拒绝，否则「没有对照一节」会被读成「对照过了」。

``post_gap`` 档从 **T+1 开盘**进，把选样日排除在窗口之外，四栏才都成立：问的是「这批
票跳完之后还继续走吗」。两档一起读：``signal_day`` 答「抓了能拿到多少」，``post_gap``
答「这点优势是不是只来自动量选位」。

持有期是网格
------------
拿票 2~10 天不固定，所以每档按 h∈{1,2,3,5,10} 出整行，不出 T+5 单一头条（memory
holding-period-is-a-grid-not-t5）。可评估日 = 面板覆盖到 T+1+h 的天数，门槛按
``MIN_DAYS + h`` 逐档算；不够就报「尚未可知」而不是「未通过」（memory
insufficient-sample-is-not-failed-control）。

统计一律复用 ``core.funnel_effect_eval``，这里只做形状适配，不新写一套口径。
"""

from __future__ import annotations

import re
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

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from core.funnel_effect_eval import Panels

# 持有期网格。拿票 2~10 天不固定，故不设单一头条窗口。
FORWARD_HORIZONS: tuple[int, ...] = (1, 2, 3, 5, 10)

# 入场档。键是行里用哪个日期当「信号日」，即 window() 的锚点。
#   signal_day：锚 previous_trade_date -> T 日开盘进。含选样日，对照循环。
#   post_gap  ：锚 trade_date          -> T+1 开盘进。排除选样日，对照成立。
ENTRY_SIGNAL_DAY = "signal_day"
ENTRY_POST_GAP = "post_gap"
ENTRY_DATE_FIELD: dict[str, str] = {
    ENTRY_SIGNAL_DAY: "previous_trade_date",
    ENTRY_POST_GAP: "trade_date",
}
# 对照只在 post_gap 档成立。signal_day 档的配对/随机对照是循环的，必须拒绝而非省略。
ENTRY_CONTROL_VALID: dict[str, bool] = {ENTRY_SIGNAL_DAY: False, ENTRY_POST_GAP: True}
CIRCULAR_REFUSAL = (
    "拒绝出对照：复盘池按「T 日涨幅 >7%」选样，本档持有窗口含 T 日，"
    "按 T-1 信息配对的对照必然落后，落后的原因就是这批票被选中的原因。"
    "本档只看绝对收益；对照口径见 post_gap 档。"
)

# 合并视图：单档位样本太薄（每天 1~19 只），故同时出两个累计口径。
# missed = 全部没进候选池的票（放开任一闸能多捞到的全集）；captured = 已进候选池的票。
MERGED_MISSED = "__missed_all__"
MERGED_CAPTURED = "__captured__"
MERGED_LABELS: dict[str, str] = {
    MERGED_MISSED: "合计·未捕获",
    MERGED_CAPTURED: "合计·已捕获",
}


def _code(value: Any) -> str:
    """统一成 6 位数字，与 ``build_panels`` 的 code 口径一致。"""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.zfill(6)[:6] if digits else ""


def capture_day_map(rows: list[dict[str, Any]], bucket: str, entry: str) -> dict[str, dict[str, Any]]:
    """把捕获行按信号日分组，塞进 ``resolve_layer`` 认的形状。

    ``formal_l4`` 填被测桶的票，``all`` 填**整个复盘池**——对照池是
    ``universe - all``，所以复盘池里其它档位的票必须一并排除：它们同样是当日
    >7% 的异动股，留在池里当对照会把「异动股 vs 异动股」读成「漏掉的 vs 同侪」。

    ``entry`` 决定锚哪个日期当信号日，即买在 T 还是 T+1 开盘。
    """
    date_field = ENTRY_DATE_FIELD[entry]
    hits: dict[str, set[str]] = {}
    pool: dict[str, set[str]] = {}
    for row in rows:
        ds = str(row.get(date_field) or "")[:10]
        code = _code(row.get("ts_code"))
        if not ds or not code:
            continue
        pool.setdefault(ds, set()).add(code)
        if _in_bucket(row, bucket):
            hits.setdefault(ds, set()).add(code)
    return {
        ds: {"formal_l4": sorted(hits.get(ds, set())), "all": sorted(codes)}
        for ds, codes in pool.items()
        if hits.get(ds)
    }


def _in_bucket(row: dict[str, Any], bucket: str) -> bool:
    """行属于被测桶吗。合并桶按 is_candidate 分，其余按 stage 精确匹配。

    ``is_candidate`` 用 ``is True`` 而非真值判断：这一列 not null default false，
    但读回来可能是 None（选列缺失），None 当 False 会把「不知道」写成「确定没捕获」。
    """
    captured = row.get("is_candidate") is True
    if bucket == MERGED_CAPTURED:
        return captured
    if bucket == MERGED_MISSED:
        return not captured
    return str(row.get("stage") or "") == bucket


def evaluate_bucket(
    rows: list[dict[str, Any]],
    panels: Panels,
    bucket: str,
    *,
    entry: str = ENTRY_POST_GAP,
    horizons: tuple[int, ...] = FORWARD_HORIZONS,
) -> dict[str, Any]:
    """单个档位在一种入场口径下的逐 h 结果。

    门槛按 ``MIN_DAYS + horizon`` 逐档算：h=10 要多十个交易日才够同样多的可评估日，
    用同一个门槛会让长窗口看着「达标」而其实只有几天（memory
    eval-days-are-trace-days-minus-horizon）。
    """
    cands = capture_day_map(rows, bucket, entry)
    control_ok = ENTRY_CONTROL_VALID[entry]
    out: dict[str, Any] = {
        "bucket": bucket,
        "label": MERGED_LABELS.get(bucket, bucket),
        "entry": entry,
        "signal_days": len(cands),
        "control_valid": control_ok,
        "horizons": {},
    }
    if not control_ok:
        out["control_refusal"] = CIRCULAR_REFUSAL
    for horizon in horizons:
        rows_h = evaluate_daily(cands, panels, horizon, status="formal_l4")
        absolute = summarize_absolute(rows_h["absolute"])
        # 可评估日 = 面板能覆盖到 T+1+h 的天数，不是 len(cands)。
        usable = len(rows_h["absolute"])
        threshold = MIN_DAYS + horizon
        cell: dict[str, Any] = {
            "usable_days": usable,
            "min_days": threshold,
            "absolute": absolute.as_dict(),
        }
        if usable < threshold:
            cell["verdict"] = f"可评估日 {usable} 天，不足 {threshold} 天，尚未可知"
            out["horizons"][str(horizon)] = cell
            continue
        if not control_ok:
            cell["verdict"] = "样本已达标；本档只出绝对收益，对照被拒绝（见 control_refusal）"
            out["horizons"][str(horizon)] = cell
            continue
        matched = summarize_group("matched", rows_h["matched"])
        controls = [summarize_group(f"control_{seed}", rows_h[f"control_{seed}"]) for seed in CONTROL_SEEDS]
        cell["matched"] = matched.as_dict()
        cell["controls"] = [c.as_dict() for c in controls]
        cell["control_gap"] = control_gap(matched, controls)
        # 胜率必须自己过一遍控制：收益与胜率两栏实测会分家。
        cell["win_control_gap"] = win_control_gap(matched, controls)
        out["horizons"][str(horizon)] = cell
    return out


def bucket_names(rows: list[dict[str, Any]], *, min_rows: int = MIN_HITS_PER_DAY) -> list[str]:
    """出现足够多次的 stage 档位 + 两个合并桶。

    单日只有 1~2 行的档位连一天都凑不满 ``MIN_HITS_PER_DAY``，列出来只会得到一串
    「尚未可知」，故按总行数先滤一遍。
    """
    counts: dict[str, int] = {}
    for row in rows:
        stage = str(row.get("stage") or "")
        if stage:
            counts[stage] = counts.get(stage, 0) + 1
    stages = sorted((s for s, n in counts.items() if n >= min_rows), key=lambda s: -counts[s])
    return [MERGED_MISSED, MERGED_CAPTURED, *stages]


def capture_forward_summary(
    rows: list[dict[str, Any]],
    panels: Panels | None,
    *,
    horizons: tuple[int, ...] = FORWARD_HORIZONS,
) -> dict[str, Any]:
    """两种入场口径 × 各档位的完整结果。缺面板时显式降级。"""
    if panels is None:
        return {
            "available": False,
            "reason": "无行情面板，无法算前瞻收益；本次不出任何收益判定",
        }
    if not rows:
        return {"available": False, "reason": "捕获表无数据"}
    buckets = bucket_names(rows)
    dates = sorted({str(r.get("trade_date") or "")[:10] for r in rows if r.get("trade_date")})
    return {
        "available": True,
        "row_count": len(rows),
        "replay_days": len(dates),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "horizons": list(horizons),
        "note": (
            f"入场两档：signal_day=T 日开盘（漏斗 T-1 决策的真实入场价，含选样日，"
            f"只出绝对收益）；post_gap=T+1 开盘（排除选样日，四栏成立）。"
            f"成本双边同扣，每日最少 {MIN_HITS_PER_DAY} 只，门槛 {MIN_DAYS}+h 天。"
            f"持有期重叠，所有 t 值都被高估。"
        ),
        "entries": {
            entry: {bucket: evaluate_bucket(rows, panels, bucket, entry=entry, horizons=horizons) for bucket in buckets}
            for entry in (ENTRY_SIGNAL_DAY, ENTRY_POST_GAP)
        },
    }


ENTRY_TITLES: dict[str, str] = {
    ENTRY_SIGNAL_DAY: "T 日开盘入场（漏斗真实入场价，只出绝对收益）",
    ENTRY_POST_GAP: "T+1 开盘入场（排除选样日，四栏成立）",
}


def forward_verdict_lines(summary: dict[str, Any]) -> list[str]:
    """报告小节。捕获率那张表旁边必须有这几行，否则「该不该抓」在数据上无法回答。"""
    if not summary.get("available"):
        return ["## 漏掉的票在 T+h 上赚不赚钱", "", f"未出：{summary.get('reason') or '未知原因'}", ""]
    lines = [
        "## 漏掉的票在 T+h 上赚不赚钱",
        "",
        f"复盘 {summary.get('replay_days')} 天、{summary.get('row_count')} 行"
        f"（{' ~ '.join(summary.get('date_range') or ['—', '—'])}）。",
        "",
        summary.get("note") or "",
        "",
    ]
    for entry, blocks in (summary.get("entries") or {}).items():
        lines += [f"### {ENTRY_TITLES.get(entry, entry)}", ""]
        for block in blocks.values():
            lines.append(f"- **{block.get('label')}**（{block.get('signal_days')} 个信号日）")
            for horizon, cell in (block.get("horizons") or {}).items():
                lines.append(f"  - T+{horizon} {_forward_cell_line(cell)}")
        lines.append("")
    return lines


def _forward_cell_line(cell: dict[str, Any]) -> str:
    """绝对、股级胜率、两条超额同时出：任何一栏单看都会得出相反结论。

    ``positive_day_pct`` 是**日级**的，不是胜率；回答「漏掉的票赚不赚钱」的是
    ``stock_win_pct``（股级，且门槛已扣往返成本）。
    """
    absolute = cell.get("absolute") or {}
    head = (
        f"绝对 {_num(absolute.get('net_pct'))}%"
        f"（股级胜率 {_num(absolute.get('stock_win_pct'))}%，"
        f"日均 {_num(absolute.get('avg_size'))} 只，{absolute.get('verdict') or '—'}）"
    )
    if "matched" not in cell:
        return f"{head}；{cell.get('verdict') or '—'}"
    matched = cell.get("matched") or {}
    gap = cell.get("control_gap") or {}
    win_gap = cell.get("win_control_gap") or {}
    return (
        f"{head}；配对超额 {_num(matched.get('excess_pct'))}pct"
        f"（t={_num(matched.get('excess_t'))}）→ {gap.get('verdict') or '样本不足'}"
        f"；胜率超额 {_num(matched.get('stock_win_excess_pct'))}pct"
        f"（t={_num(matched.get('stock_win_excess_t'))}）→ {win_gap.get('verdict') or '样本不足'}"
    )


def _num(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f}"
