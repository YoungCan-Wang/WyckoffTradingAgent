"""Markdown report rendering for strong-move replay reviews."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from core.funnel_taxonomy import (
    REVIEW_STAGE_BASE_REJECT,
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_DATA_FAIL,
    REVIEW_STAGE_OUT_OF_POOL,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_TRIGGER_MISS,
)
from core.review_shadow_lanes import shadow_lane_label

CAPTURE_STAGE_ORDER = (
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_TRIGGER_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_BASE_REJECT,
)
"""出捕获率的档位顺序:先漏斗内部各档,基础准入淘汰放最后。

池外/数据失败不出比率——它们在分母那一侧根本没有对应总体。
"""

PRE_FUNNEL_STAGES = (REVIEW_STAGE_BASE_REJECT, REVIEW_STAGE_OUT_OF_POOL, REVIEW_STAGE_DATA_FAIL)

MIN_EXPECTED_HITS_FOR_LIFT = 3.0
"""某一档在同基数下的期望命中数低于这个值就不出倍数,只出原始只数。

单日里候选池分母只有 100 上下,基数率常在 1%~2%,期望命中就 1~2 只。这时候命中 0 只
写成「0.00x」会被当成结论,可实测 21 天里候选池单日倍数在 0.00x~2.44x 之间摆,合起来
才是 1.36x。取 3 是因为期望 3 时泊松下「一只都没中」的概率恰好 e^-3≈0.050——再低于此,
连空档都说明不了什么,倍数就没有可读性。分母本身照常给出,省的只是那个比值。
"""


def short_code_list(rows: list[dict[str, Any]], limit: int = 8) -> str:
    shown = [f"{row['code']}{row['name']}" for row in rows[:limit]]
    if len(rows) > limit:
        shown.append(f"等{len(rows)}只")
    return "、".join(shown) if shown else "无"


def build_focus_lines(
    rows: list[dict[str, Any]],
    today: date,
    previous_trade_date: date,
    stats: dict[str, Any] | None = None,
) -> list[str]:
    total = max(len(rows), 1)
    stage_rows = _group_stage_rows(rows)
    lines = ["**重点归因**", _result_selected_caveat(stats)]
    lines.extend(_date_gap_lines(today, previous_trade_date))
    lines.extend(_stage_focus_lines(stage_rows, total))
    return lines


def _result_selected_caveat(stats: dict[str, Any] | None = None) -> str:
    """先说清这一段的只数是按结果选的,否则会被当成淘汰率读。

    样本定义是「今日收盘>+7% 且前一日<+3%」——先有结果再回溯原因。被同一道闸门
    挡住却没涨的票不进样本,所以「39 只被基础准入淘汰」这类数字本身推不出闸门误伤。
    上面的「逐档捕获率」补了同条件分母,能回答概率高低;但要判断某车道该不该补强,
    仍得走全量打标签 + 同动量随机对照,那是 workflows/review_shadow_backtest.py 的事。
    """
    if _has_denominator(stats):
        return (
            "- **样本口径**：这一段的只数是按「今日涨幅>+7%」选样后回溯的，本身没有分母，"
            "只能定位单票卡点；要看概率高低请看上面的「逐档捕获率」，"
            "要判断某车道该不该补强仍需全量打标签 + 同动量随机对照（影子回放）。"
        )
    return (
        "- **样本口径**：本报告先按「今日涨幅>+7%」选样、再回溯卡在哪一层，"
        "被同样阈值挡住却没涨的票不在样本内，因此下面每一档的只数都**没有分母**"
        "（本次未取到全市场信号日涨幅，逐档捕获率已整段省略）。"
        "它只能定位单票卡点，不能作为放宽任何阈值或车道的依据；"
        "「某车道该不该补强」要走全量打标签 + 同动量随机对照（影子回放）。"
    )


def _has_denominator(stats: dict[str, Any] | None) -> bool:
    if not stats:
        return False
    return isinstance(stats.get("stage_denominator"), dict) and int(stats.get("denominator_total", 0) or 0) > 0


def build_report_lines(
    rows: list[dict[str, Any]],
    stage_counter: Counter[str],
    today: date,
    previous_trade_date: date,
    end_trade_date: str,
    stats: dict[str, Any] | None = None,
) -> list[str]:
    summary = " | ".join([f"{key}{value}" for key, value in stage_counter.items()]) or "无"
    lines = [
        f"**今日**: {today}",
        f"**前一日漏斗**: {end_trade_date}",
        f"**今日收盘涨幅>+7%且前一交易日收盘涨幅<+3%股票数**: {len(rows)}",
    ]
    if stats:
        if stats.get("context_source"):
            lines.append(f"**归因数据源**: {stats['context_source']}")
        headline = _buyable_capture_headline(stats)
        if headline:
            lines.append(headline)
        stats_line = (
            f"**漏斗全链路追踪**: 前一日候选 {stats['candidate']}/{stats['total']} | "
            f"跟踪表记录 {stats.get('tracked_previous_day', stats['recommended'])}/{stats['total']} | "
            f"AI正式推荐 {stats.get('ai_recommended_previous_day', 0)}/{stats['total']}"
        )
        lines.append(stats_line)
        if stats.get("shadow"):
            lines.append(
                f"**影子召回（不进推荐）**: {stats['shadow']}/{stats['total']} | "
                f"其中次日开盘可交易 {stats.get('shadow_open_executable', 0)}/{stats['shadow']} | "
                "这是事后命中统计，不是影子池规模或次日买入清单"
            )
        execution_line = _execution_scope_line(stats)
        if execution_line:
            lines.append(execution_line)
    lines.extend(
        [
            f"**结果汇总**: {summary}",
            *build_capture_rate_lines(stage_counter, stats),
            "",
            *build_focus_lines(rows, today=today, previous_trade_date=previous_trade_date, stats=stats),
            "",
            "**逐票复盘（前一日候选链路状态与原因）**",
            "",
        ]
    )
    lines.extend(_detail_lines(rows))
    return lines


def _buyable_capture_headline(stats: dict[str, Any]) -> str:
    """把「可交易样本里前日候选占几只」提到报告头。

    这是全篇唯一分子分母同池的比率:分母是「昨天基础准入通过、今天涨了、且次日
    还能按≤+4%开盘价买到」的票,分子是其中前一日真在候选池里的。其它数字(影子
    召回 35/98、可交易 32/35)都是事后命中计数,分母是涨幅筛出来的。
    实测 2026-09-01 这行是 1/54,而它原先埋在可交易口径的第三段。
    """
    if stats.get("execution_available", 0) <= 0:
        return ""
    executable = stats.get("open_executable", 0)
    if executable <= 0:
        return ""
    captured = stats.get("candidate_open_executable", 0)
    rate = captured / executable * 100.0
    return (
        f"**可买到且被捕获**: {captured}/{executable}（{rate:.1f}%）"
        " ← 全篇唯一分母同池的比率：次日开盘还买得到的强势票里，前一日真在候选池的有几只"
    )


def build_capture_rate_lines(stage_counter: Counter[str], stats: dict[str, Any] | None) -> list[str]:
    """逐档捕获率:分子分母共用「信号日涨幅<+3% 的全市场票」这一个总体。

    分子那条「前一日<+3%」会按信号日涨幅筛人,而候选池装的是动量票、信号日自己
    更容易已经涨过 3%(实测候选 33.8% 越线、非候选 L1 存活只有 11.9%)。分母若不
    施加同一条件,候选池捕获率会被系统性压低——实测能把 1.89x 印成 1.03x,而那个
    数看上去比没有分母还像结论。所以分母必须同条件,拿不到就一个比率都不出。

    两个基数分开给:漏斗内部各档跟「基础准入通过者」比,基础准入淘汰跟全市场比。
    拿基础准入淘汰去跟全市场混算会把候选池的比率稀释掉一半以上。
    """
    if not stats:
        return []
    denominator = stats.get("stage_denominator")
    total = int(stats.get("denominator_total", 0) or 0)
    if not isinstance(denominator, dict) or total <= 0:
        return []
    max_previous = float(stats.get("denominator_max_previous_pct", 0.0) or 0.0)
    counts = {str(key): int(value) for key, value in denominator.items()}
    pre_funnel = sum(counts.get(stage, 0) for stage in PRE_FUNNEL_STAGES)
    l1_base = total - pre_funnel
    matched_numerator = sum(stage_counter.get(stage, 0) for stage in counts)
    l1_numerator = sum(stage_counter.get(stage, 0) for stage in counts if stage not in PRE_FUNNEL_STAGES)

    lines = [
        "",
        f"**逐档捕获率（分子分母同池：信号日涨幅<{max_previous:.0f}% 的票）**",
        _capture_scope_note(max_previous),
        f"- **全市场基数**：{matched_numerator}/{total}（{_rate(matched_numerator, total)}）今日涨超+7%",
    ]
    if l1_base > 0:
        lines.append(f"- **基础准入通过者基数**：{l1_numerator}/{l1_base}（{_rate(l1_numerator, l1_base)}）今日涨超+7%")
    for stage in CAPTURE_STAGE_ORDER:
        stage_denominator = counts.get(stage, 0)
        if stage_denominator <= 0:
            continue
        hit = stage_counter.get(stage, 0)
        base_rate = matched_numerator / total if stage in PRE_FUNNEL_STAGES else _safe_ratio(l1_numerator, l1_base)
        base_label = "全市场" if stage in PRE_FUNNEL_STAGES else "基础准入通过者"
        lines.append(
            f"- {stage}：{hit}/{stage_denominator}（{_rate(hit, stage_denominator)}）"
            f"{_lift_text(hit / stage_denominator, base_rate, base_label, stage_denominator * base_rate)}"
        )
    return lines


def _capture_scope_note(max_previous: float) -> str:
    return (
        f"- 分子分母都只看「信号日涨幅<{max_previous:.0f}%」的票，所以这一段回答的是"
        "「昨天没怎么动的票里，进到某一档的今天涨超+7% 的概率有多高」，"
        "不覆盖昨天已经冲高的票；倍数是同一档对同一基数的比值，不是绝对胜率。"
        "单日各档期望命中常只有 1~2 只，倍数摆动很大，跨日累计口径看 "
        "scripts/evaluate_review_capture_forward.py。"
    )


def _rate(hit: int, base: int) -> str:
    if base <= 0:
        return "无分母"
    return f"{hit / base * 100.0:.2f}%"


def _safe_ratio(hit: int, base: int) -> float:
    return hit / base if base > 0 else 0.0


def _lift_text(rate: float, base_rate: float, base_label: str, expected_hits: float) -> str:
    if base_rate <= 0:
        return ""
    if expected_hits < MIN_EXPECTED_HITS_FOR_LIFT:
        return f" ← 同基数下期望命中{expected_hits:.1f}只，单日读不出倍数"
    return f" ← 相对{base_label} {rate / base_rate:.2f}x"


def _execution_scope_line(stats: dict[str, Any]) -> str:
    if stats.get("execution_available", 0) <= 0:
        return ""
    eligible = stats.get("l1_eligible", 0)
    executable = stats.get("open_executable", 0)
    captured = stats.get("candidate_open_executable", 0)
    intraday = stats.get("intraday_executable", 0)
    intraday_captured = stats.get("candidate_intraday_executable", 0)
    return (
        f"**可交易复盘口径**: 前日基础准入 {eligible}/{stats.get('total', 0)} | "
        f"次日开盘≤+4%且非一字板 {executable}/{eligible} | 可交易样本前日候选 {captured}/{executable} | "
        f"盘中曾给≤+4%价格 {intraday}/{eligible} | 其中前日候选 {intraday_captured}/{intraday}"
    )


def _group_stage_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    stage_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        stage_rows.setdefault(row["stage"], []).append(row)
    return stage_rows


def _date_gap_lines(today: date, previous_trade_date: date) -> list[str]:
    gap_days = (today - previous_trade_date).days
    if gap_days <= 1:
        return []
    return [
        f"- **日期间隔**：{previous_trade_date} 收盘后到 {today} 之间跨 {gap_days} 个自然日，节假日/周末消息驱动的跳空异动，本来就很难由前一交易日日线结构提前捕获。"
    ]


def _stage_focus_lines(stage_rows: dict[str, list[dict[str, Any]]], total: int) -> list[str]:
    lines: list[str] = []
    lines.extend(_candidate_hit_focus(stage_rows.get(REVIEW_STAGE_CANDIDATE_HIT, [])))
    lines.extend(_strength_miss_focus(stage_rows.get(REVIEW_STAGE_STRENGTH_MISS, []), total))
    lines.extend(_risk_focus(stage_rows.get(REVIEW_STAGE_RISK_BLOCK, [])))
    lines.extend(_trigger_miss_focus(stage_rows.get(REVIEW_STAGE_TRIGGER_MISS, [])))
    lines.extend(_theme_miss_focus(stage_rows.get(REVIEW_STAGE_THEME_MISS, [])))
    lines.extend(_base_reject_focus(stage_rows.get(REVIEW_STAGE_BASE_REJECT, [])))
    lines.extend(_trigger_hit_focus(stage_rows.get(REVIEW_STAGE_TRIGGER_HIT, [])))
    return lines


def _candidate_hit_focus(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    return [
        f"- **候选池已捕获**：{short_code_list(rows)}。这些票已进入前一日多路候选池，后续重点核对 AI 配额、跨日 confirmed 和 OMS 风控是否挡住。"
    ]


def _strength_miss_focus(rows: list[dict[str, Any]], total: int) -> list[str]:
    if not rows:
        return []
    pct = len(rows) / total * 100.0
    return [
        f"- **未入候选池：结构强度不足**：{len(rows)} / {total}（{pct:.1f}%）没有被主线、趋势回踩、趋势突破、板块强势或 Wyckoff 结构车道接住。"
    ]


def _risk_focus(rows: list[dict[str, Any]]) -> list[str]:
    """不再说「被硬拦截」。

    生产里离场信号根本不是闸门:四路候选生产者只有正式威科夫那一路读 exit_signals
    (core/wyckoff_engine.py:2349),而且是 -35 分的软扣分;车道/alpha/主线三路
    完全不读。2026-09-04 落到这一档的 147 只票 trigger_labels 全为空——没有买点
    可拦,这个标签只是级联顺序的产物(memory funnel-cascade-label-is-not-cause)。
    同一天有 19 只带 stop_loss 的票照样进了候选池,其中 16 只走的是不读离场信号的车道。
    """
    if not rows:
        return []
    blocking = [row for row in rows if row.get("trigger_labels")]
    if not blocking:
        return [
            f"- **带风控信号**：{short_code_list(rows)}。这一档买点本来就没触发，"
            "所以风控没拦下任何东西，标签只反映级联顺序。"
            "离场信号在生产里是正式威科夫那一路的软扣分，车道/alpha/主线三路不读它。"
        ]
    return [
        f"- **带风控信号**：{short_code_list(rows)}。其中 {len(blocking)} 只买点已触发而带离场信号，"
        "这几只才真被扣了分；要判断这个扣分是否伤了胜率，需按同动量对照比前向收益。"
    ]


def _trigger_miss_focus(rows: list[dict[str, Any]]) -> list[str]:
    """不再从这一档提「补强爆发前夜车道」。

    原措辞「适合检查爆发前夜压缩/试盘类车道是否需要补强」是在结果选样的证据上
    直接提改动方案,而同一份报告的「基础准入淘汰」那档已经写了「不建议为涨停复盘
    反向放宽」——同样的证据强度,两档结论必须一致。
    """
    if not rows:
        return []
    return [
        f"- **买点未确认**：{short_code_list(rows)}。已过结构层、买点未触发。"
        "同口径下没涨的票不在样本内，所以这里看不出买点阈值是松还是紧；"
        "要判断先跑影子回放取 T+5 超额与同动量对照。"
    ]


def _theme_miss_focus(rows: list[dict[str, Any]]) -> list[str]:
    """题材层不一定是这一档的约束点,得先把并发的离场信号摊出来。

    级联在 L3 之前不查离场信号,所以「题材共振不足」这个标签会把同时带 stop_loss
    的票一起收进来。2026-09-04 全市场这一档 1678 只,其中 1147 只(68.4%)带
    stop_loss——对这三分之二来说,就算题材层放它过去,下一道也会扣分,改题材映射
    动不了它们。只报总数会把改动引到不是约束点的那一层。
    """
    if not rows:
        return []
    risky = [row for row in rows if row.get("risk_signal")]
    line = f"- **题材共振不足**：{short_code_list(rows)}。"
    if risky:
        line += (
            f"注意其中 {len(risky)}/{len(rows)} 只同时带离场信号（{short_code_list(risky)}），"
            "对这部分票题材层不是唯一约束，只改题材映射动不了它们；"
            "先看剩下那些干净的票再决定要不要碰题材层。"
        )
    else:
        line += "这一档都没带离场信号，可以检查题材映射、主线热度和板块强势车道覆盖。"
    return [line]


def _base_reject_focus(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    return [f"- **基础准入淘汰**：{short_code_list(rows)}。主要是成交额/基础流动性，不建议为涨停复盘反向放宽。"]


def _trigger_hit_focus(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    return [f"- **买点已确认**：{short_code_list(rows)}。这类不是形态漏检，后续应核对是否被 AI 配额或风控环节挡住。"]


def _detail_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        recommendation = str(row.get("recommendation", "")).strip()
        suffix = f" | {recommendation}" if recommendation else ""
        state = _state_suffix(row)
        lines.append(f"• {row['code']} {row['name']} | {row['stage']} | {row['reason']}{state}{suffix}")
    return lines


def _state_suffix(row: dict[str, Any]) -> str:
    states: list[str] = []
    if row.get("shadow_lane"):
        states.append(f"影子={shadow_lane_label(str(row['shadow_lane']))}")
    if row.get("trigger_labels"):
        states.append(f"买点={'、'.join(str(x) for x in row['trigger_labels'])}")
    if row.get("risk_signal"):
        states.append(f"风控={row['risk_signal']}")
    elif row.get("risk_evaluated") is False:
        # 空的 risk_signal 有两种来源:查过没事,和从来没查。离场信号只对 L2 通过池
        # 算,L2 未过的票落到这里恒为空,不标出来就会被读成「风控干净」。
        states.append("风控=未评估")
    if row.get("tracked_previous_day"):
        status = "、".join(str(x) for x in row.get("candidate_statuses") or []) or "已跟踪"
        states.append(f"跟踪状态={status}")
    if row.get("execution_available"):
        open_gap = _pct(row.get("open_gap_pct"))
        low_gap = _pct(row.get("low_gap_pct"))
        states.append(f"次日开盘{open_gap}，最低{low_gap}")
    return f" | {' | '.join(states)}" if states else ""


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "未知"
