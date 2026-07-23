"""Markdown report rendering for strong-move replay reviews."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from core.funnel_taxonomy import (
    REVIEW_STAGE_BASE_REJECT,
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_TRIGGER_MISS,
)


def short_code_list(rows: list[dict[str, Any]], limit: int = 8) -> str:
    shown = [f"{row['code']}{row['name']}" for row in rows[:limit]]
    if len(rows) > limit:
        shown.append(f"等{len(rows)}只")
    return "、".join(shown) if shown else "无"


def build_focus_lines(rows: list[dict[str, Any]], today: date, previous_trade_date: date) -> list[str]:
    total = max(len(rows), 1)
    stage_rows = _group_stage_rows(rows)
    lines = ["**重点归因**"]
    lines.extend(_date_gap_lines(today, previous_trade_date))
    lines.extend(_stage_focus_lines(stage_rows, total))
    return lines


def build_report_lines(
    rows: list[dict[str, Any]],
    stage_counter: Counter[str],
    today: date,
    previous_trade_date: date,
    end_trade_date: str,
    stats: dict[str, int] | None = None,
) -> list[str]:
    summary = " | ".join([f"{key}{value}" for key, value in stage_counter.items()]) or "无"
    lines = [
        f"**今日**: {today}",
        f"**前一日漏斗**: {end_trade_date}",
        f"**今日收盘涨幅>+7%且前一交易日收盘涨幅<+3%股票数**: {len(rows)}",
    ]
    if stats:
        stats_line = (
            f"**漏斗全链路追踪**: 前一日候选 {stats['candidate']}/{stats['total']} | "
            f"正式推荐 {stats['recommended']}/{stats['total']}"
        )
        lines.append(stats_line)
        execution_line = _execution_scope_line(stats)
        if execution_line:
            lines.append(execution_line)
    lines.extend(
        [
            f"**结果汇总**: {summary}",
            "",
            *build_focus_lines(rows, today=today, previous_trade_date=previous_trade_date),
            "",
            "**逐票复盘（前一日候选链路状态与原因）**",
            "",
        ]
    )
    lines.extend(_detail_lines(rows))
    return lines


def _execution_scope_line(stats: dict[str, int]) -> str:
    if stats.get("execution_available", 0) <= 0:
        return ""
    eligible = stats.get("l1_eligible", 0)
    executable = stats.get("open_executable", 0)
    captured = stats.get("candidate_open_executable", 0)
    return (
        f"**可交易复盘口径**: 前日基础准入 {eligible}/{stats.get('total', 0)} | "
        f"次日开盘≤+4%且非一字板 {executable}/{eligible} | 可交易样本前日候选 {captured}/{executable}"
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
    if not rows:
        return []
    return [
        f"- **风控拦截优先复盘**：{short_code_list(rows)}。这些票被结构止损/派发信号硬拦截，适合单独检查止损是否对强修复过敏。"
    ]


def _trigger_miss_focus(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    return [
        f"- **买点未确认**：{short_code_list(rows)}。这些票已有结构基础但未触发买点确认，适合检查“爆发前夜压缩/试盘”类车道是否需要补强。"
    ]


def _theme_miss_focus(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    return [f"- **题材共振不足**：{short_code_list(rows)}。优先检查题材映射、主线热度和板块强势车道覆盖。"]


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
        lines.append(f"• {row['code']} {row['name']} | {row['stage']} | {row['reason']}{suffix}")
    return lines
