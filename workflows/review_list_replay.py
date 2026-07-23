"""Review-list replay workflow."""

from __future__ import annotations

import contextlib
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from core.candidate_ranker import TRIGGER_LABELS
from core.candidate_tracks import best_candidate_entry_map
from core.funnel_taxonomy import (
    REVIEW_STAGE_BASE_REJECT,
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_RISK_BLOCK,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
    REVIEW_STAGE_TRIGGER_HIT,
    REVIEW_STAGE_TRIGGER_MISS,
    lane_label,
)
from core.wyckoff_engine import (
    FunnelConfig,
    Layer2EvaluationContext,
    build_layer2_evaluation_context,
    sort_by_date_if_needed,
)
from utils.feishu import send_feishu_notification
from workflows.review_big_gainers import execution_snapshot, is_target_cn_board, load_today_review_pool
from workflows.review_recommendation_lookup import format_recommendation_history, load_recommendation_lookup
from workflows.review_report_render import build_report_lines
from workflows.wyckoff_funnel import run_funnel_job


@dataclass(frozen=True)
class ReviewDates:
    today: date
    previous_trade_date: date
    today_window: object


@dataclass(frozen=True)
class ReplayContext:
    cfg: FunnelConfig
    all_symbol_set: set[str]
    name_map: dict[str, str]
    market_cap_map: dict[str, float]
    sector_map: dict[str, str]
    df_map: dict[str, pd.DataFrame]
    l1_set: set[str]
    l2_set: set[str]
    l3_set: set[str]
    end_trade_date: str
    l2_ctx: Layer2EvaluationContext
    hit_map: dict[str, list[str]]
    blocked_exit_map: dict[str, dict]
    candidate_entry_map: dict[str, dict]


def run_review_list_replay(webhook: str, log=print) -> int:
    if not webhook:
        log("[review] FEISHU_WEBHOOK_URL 未配置")
        return 2
    log("[review] 获取当日收盘涨幅>+7%且前一交易日收盘涨幅<+3%股票...")
    dates = resolve_review_dates()
    log(f"[review] 今日: {dates.today}, 前一交易日: {dates.previous_trade_date}")
    name_map_today, all_codes = load_today_pool()
    pool = load_today_review_pool(all_codes, name_map_today, dates.today_window, log=log)
    execution_map = {code: execution_snapshot(pool.frames.get(code)) for code in pool.codes}
    return _run_review_for_codes(webhook, pool.codes, dates, log, execution_map)


def resolve_review_dates() -> ReviewDates:
    from integrations.fetch_a_share_csv import resolve_trading_window
    from utils.trading_clock import resolve_end_calendar_day

    end_calendar_day = resolve_end_calendar_day()
    today_window = resolve_trading_window(end_calendar_day=end_calendar_day, trading_days=3)
    today = today_window.end_trade_date
    previous_window = resolve_trading_window(end_calendar_day=today - timedelta(days=1), trading_days=1)
    return ReviewDates(today=today, previous_trade_date=previous_window.end_trade_date, today_window=today_window)


def load_today_pool() -> tuple[dict[str, str], list[str]]:
    from integrations.fetch_a_share_csv import get_stocks_by_board

    stock_items = get_stocks_by_board("all")
    name_map_today = {
        str(item.get("code", "")).strip(): str(item.get("name", "")).strip()
        for item in stock_items
        if isinstance(item, dict) and str(item.get("code", "")).strip()
    }
    return name_map_today, sorted(name_map_today.keys())


def run_previous_funnel(previous_trade_date: date, log=print) -> tuple[dict, dict]:
    log(f"[review] 回放前一交易日 ({previous_trade_date}) 漏斗...")
    original_end_day = os.getenv("END_CALENDAR_DAY", "")
    os.environ["END_CALENDAR_DAY"] = previous_trade_date.strftime("%Y-%m-%d")
    try:
        return run_funnel_job(include_debug_context=True, direct_source=True)
    finally:
        if original_end_day:
            os.environ["END_CALENDAR_DAY"] = original_end_day
        else:
            os.environ.pop("END_CALENDAR_DAY", None)


def replay_context(triggers: dict, metrics: dict, log=print) -> ReplayContext | None:
    debug = metrics.get("_debug", {}) or {}
    if not debug:
        log("[review] 缺少调试上下文，无法复盘")
        return None
    df_map = debug.get("all_df_map", {}) or {}
    return ReplayContext(
        cfg=debug.get("cfg"),
        all_symbol_set=set(str(x) for x in (debug.get("all_symbols", []) or [])),
        name_map=debug.get("name_map", {}) or {},
        market_cap_map=debug.get("market_cap_map", {}) or {},
        sector_map=debug.get("sector_map", {}) or {},
        df_map=df_map,
        l1_set=set(str(x) for x in (debug.get("layer1_symbols", []) or [])),
        l2_set=set(str(x) for x in (debug.get("layer2_symbols", []) or [])),
        l3_set=set(str(x) for x in (debug.get("layer3_symbols_raw", []) or [])),
        end_trade_date=str(debug.get("end_trade_date", "未知")),
        l2_ctx=build_layer2_context(
            df_map=df_map,
            bench_df=debug.get("bench_df"),
            cfg=debug.get("cfg"),
        ),
        hit_map=build_hit_map(triggers),
        blocked_exit_map=blocked_exit_signal_map(metrics.get("exit_signals", {}) or {}),
        candidate_entry_map=build_candidate_entry_map(metrics.get("candidate_entries", []) or []),
    )


def classify_review_code(code: str, ctx: ReplayContext) -> tuple[str, str, str]:
    name = str(ctx.name_map.get(code, code)).strip() or code
    if code not in ctx.all_symbol_set:
        return name, "池外", "不在当日全市场去ST股票池"
    if code not in ctx.df_map:
        return name, "数据失败", "日线拉取失败/超时"
    if code not in ctx.l1_set:
        return (
            name,
            REVIEW_STAGE_BASE_REJECT,
            explain_l1_fail(code, ctx.cfg, ctx.name_map, ctx.market_cap_map, ctx.df_map),
        )
    if code in ctx.candidate_entry_map:
        return name, REVIEW_STAGE_CANDIDATE_HIT, explain_candidate_entry(code, ctx.candidate_entry_map)
    if code not in ctx.l2_set:
        return name, REVIEW_STAGE_STRENGTH_MISS, explain_l2_fail(code, ctx.cfg, ctx.df_map, ctx.l2_ctx)
    if code not in ctx.l3_set:
        return name, REVIEW_STAGE_THEME_MISS, f"题材/行业共振不足（{ctx.sector_map.get(code, '未知行业')}）"
    if code in ctx.blocked_exit_map:
        return name, REVIEW_STAGE_RISK_BLOCK, explain_risk_reject(code, ctx.blocked_exit_map, ctx.hit_map)
    if code in ctx.hit_map:
        return name, REVIEW_STAGE_TRIGGER_HIT, "、".join(ctx.hit_map.get(code, []))
    return name, REVIEW_STAGE_TRIGGER_MISS, "未触发 Spring/LPS/EVR/SOS 等买点确认"


def build_layer2_context(
    df_map: dict[str, pd.DataFrame],
    bench_df: pd.DataFrame | None,
    cfg: FunnelConfig,
) -> Layer2EvaluationContext:
    symbols = list(df_map)
    return build_layer2_evaluation_context(symbols, df_map, bench_df, cfg, rps_universe=symbols)


def build_hit_map(triggers: dict[str, list[tuple[str, float]]]) -> dict[str, list[str]]:
    hit_map: dict[str, list[str]] = {}
    for trig, label in TRIGGER_LABELS.items():
        for code, _ in triggers.get(trig, []):
            hit_map.setdefault(str(code), [])
            if label not in hit_map[str(code)]:
                hit_map[str(code)].append(label)
    return hit_map


def blocked_exit_signal_map(exit_signals: dict[str, dict] | None) -> dict[str, dict]:
    blocked: dict[str, dict] = {}
    for code, raw in (exit_signals or {}).items():
        signal = str((raw or {}).get("signal", "")).strip()
        if signal in {"stop_loss", "distribution_warning", "upthrust_warning"}:
            blocked[str(code)] = dict(raw or {})
    return blocked


def build_candidate_entry_map(entries: list[dict]) -> dict[str, dict]:
    return best_candidate_entry_map(entries)


def explain_candidate_entry(code: str, entry_map: dict[str, dict]) -> str:
    entry = entry_map.get(code, {}) or {}
    entry_type = str(entry.get("entry_type") or entry.get("signal_key") or "candidate").strip()
    score = float(entry.get("score", 0.0) or 0.0)
    parts = [f"候选车道: {lane_label(entry_type) or entry_type}", f"score={score:.2f}"]
    for key in ("opportunity", "timing", "risk"):
        value = str(entry.get(key, "") or "").strip()
        if value:
            parts.append(value)
    return " | ".join(parts)


def explain_l1_fail(
    code: str,
    cfg: FunnelConfig,
    name_map: dict[str, str],
    market_cap_map: dict[str, float],
    df_map: dict[str, pd.DataFrame],
) -> str:
    if not is_target_cn_board(code):
        return "非A股目标板块代码"
    if "ST" in str(name_map.get(code, "")).upper():
        return "ST股票"
    cap_reason = _market_cap_fail_reason(code, cfg, market_cap_map)
    if cap_reason:
        return cap_reason
    return _amount_fail_reason(code, cfg, df_map)


def explain_l2_fail(
    code: str,
    cfg: FunnelConfig,
    df_map: dict[str, pd.DataFrame],
    ctx: Layer2EvaluationContext,
) -> str:
    from core.wyckoff_engine import layer2_strength_detailed

    df = df_map.get(code)
    if df is None or len(df) < cfg.ma_long:
        return f"历史长度不足: < MA{cfg.ma_long}"
    rejections: dict[str, str] = {}
    passed, channel_map, _ = layer2_strength_detailed(
        [code],
        df_map,
        None,
        cfg,
        rejections=rejections,
        evaluation_context=ctx,
    )
    if passed:
        return f"结构强度通道已通过[{channel_map.get(code, '未知通道')}]，应在题材共振或买点确认阶段被拦截"
    return f"结构强度不足：{rejections.get(code, '八通道均未通过')}"


def _build_replay_row(
    code: str,
    ctx: ReplayContext,
    today: date,
    prev_date_str: str,
    recommendation_lookup: dict,
    recommendation_error: Any,
    execution_map: dict[str, dict[str, object]],
) -> tuple[dict, str, bool, bool]:
    from workflows.review_recommendation_lookup import normalize_code6, normalize_recommend_date

    name, stage, reason = classify_review_code(code, ctx)
    is_candidate = code in ctx.candidate_entry_map

    rec_records = recommendation_lookup.get(normalize_code6(code), [])
    is_recommended = any(normalize_recommend_date(r.get("recommend_date")) == prev_date_str for r in rec_records)

    rec_text = format_recommendation_history(code, recommendation_lookup, recommendation_error, exclude_date=today)
    execution = execution_map.get(code, {})

    row = {
        "code": code,
        "name": name,
        "stage": stage,
        "reason": reason,
        "recommendation": rec_text,
        "l1_eligible": code in ctx.l1_set,
        "open_executable": bool(execution.get("executable")),
        "execution_available": bool(execution.get("available")),
        "open_gap_pct": execution.get("open_gap_pct"),
        "execution_reason": str(execution.get("reason", "") or ""),
    }
    return row, stage, is_candidate, is_recommended


def build_replay_rows(
    review_codes: list[str],
    ctx: ReplayContext,
    today: date,
    previous_trade_date: date,
    execution_map: dict[str, dict[str, object]] | None = None,
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, int]]:
    recommendation_lookup, recommendation_error = load_recommendation_lookup(review_codes)
    execution_map = execution_map or {}

    rows: list[dict[str, Any]] = []
    stage_counter: Counter[str] = Counter()

    cand_count = 0
    rec_count = 0

    prev_date_str = previous_trade_date.strftime("%Y-%m-%d")

    for code in review_codes:
        row, stage, is_candidate, is_recommended = _build_replay_row(
            code=code,
            ctx=ctx,
            today=today,
            prev_date_str=prev_date_str,
            recommendation_lookup=recommendation_lookup,
            recommendation_error=recommendation_error,
            execution_map=execution_map,
        )
        rows.append(row)
        stage_counter[stage] += 1
        if is_candidate:
            cand_count += 1
        if is_recommended:
            rec_count += 1

    stats = {
        "candidate": cand_count,
        "recommended": rec_count,
        "total": len(review_codes),
        "l1_eligible": sum(bool(row["l1_eligible"]) for row in rows),
        "open_executable": sum(bool(row["l1_eligible"]) and bool(row["open_executable"]) for row in rows),
        "candidate_open_executable": sum(
            row["code"] in ctx.candidate_entry_map and bool(row["l1_eligible"]) and bool(row["open_executable"])
            for row in rows
        ),
        "execution_available": sum(bool(row["execution_available"]) for row in rows),
    }
    return rows, stage_counter, stats


def explain_risk_reject(code: str, blocked_exit_map: dict[str, dict], hit_map: dict[str, list[str]]) -> str:
    exit_sig = blocked_exit_map.get(code, {}) or {}
    parts = [_signal_label(exit_sig)]
    price = exit_sig.get("price")
    if price is not None:
        with contextlib.suppress(Exception):
            parts.append(f"参考价={float(price):.2f}")
    trigger_labels = "、".join(hit_map.get(code, []))
    if trigger_labels:
        parts.append(f"买点确认={trigger_labels}")
    reason = str(exit_sig.get("reason", "")).strip()
    if reason:
        parts.append(reason)
    return " | ".join(parts)


def send_replay_report(
    webhook: str,
    rows: list[dict[str, Any]],
    stage_counter: Counter[str],
    dates: ReviewDates,
    end_trade_date: str,
    stats: dict[str, int] | None = None,
) -> bool:
    lines = build_report_lines(
        rows=rows,
        stage_counter=stage_counter,
        today=dates.today,
        previous_trade_date=dates.previous_trade_date,
        end_trade_date=end_trade_date,
        stats=stats,
    )
    return send_feishu_notification(webhook, "🔍 强势股复盘：今日异动为何未在前一日漏斗捕获", "\n".join(lines))


def _run_review_for_codes(
    webhook: str,
    review_codes: list[str],
    dates: ReviewDates,
    log,
    execution_map: dict[str, dict[str, object]] | None = None,
) -> int:
    if not review_codes:
        log("[review] 今日无满足收盘涨幅 > 7% 且前一交易日收盘涨幅 < 3% 的股票，跳过")
        send_empty_review(webhook, dates.today)
        return 0
    log(f"[review] 今日发现满足强势复盘池股票 {len(review_codes)} 只: {', '.join(review_codes)}")
    triggers, metrics = run_previous_funnel(dates.previous_trade_date, log=log)
    ctx = replay_context(triggers, metrics, log=log)
    if ctx is None:
        return 3
    rows, stage_counter, stats = build_replay_rows(
        review_codes,
        ctx,
        dates.today,
        dates.previous_trade_date,
        execution_map,
    )
    ok = send_replay_report(webhook, rows, stage_counter, dates, ctx.end_trade_date, stats)
    log(f"[review] feishu_sent={ok}")
    return 0 if ok else 4


def send_empty_review(webhook: str, today: date) -> None:
    send_feishu_notification(
        webhook,
        "🔍 强势股复盘",
        f"交易日 {today}：今日无满足收盘涨幅 > 7% 且前一交易日收盘涨幅 < 3% 的全市场股票",
    )


def _market_cap_fail_reason(code: str, cfg: FunnelConfig, market_cap_map: dict[str, float]) -> str:
    if not market_cap_map:
        return ""
    cap = float(market_cap_map.get(code, 0.0) or 0.0)
    if cap < cfg.min_market_cap_yi:
        return f"市值不足: {cap:.2f}亿 < {cfg.min_market_cap_yi:.2f}亿"
    return ""


def _amount_fail_reason(code: str, cfg: FunnelConfig, df_map: dict[str, pd.DataFrame]) -> str:
    df = df_map.get(code)
    if df is None or df.empty:
        return "缺少日线数据"
    sorted_df = sort_by_date_if_needed(df)
    if "amount" not in sorted_df.columns:
        return "未通过基础准入（综合条件不满足）"
    avg_amt = pd.to_numeric(sorted_df["amount"], errors="coerce").tail(cfg.amount_avg_window).mean()
    if pd.notna(avg_amt) and float(avg_amt) < cfg.min_avg_amount_wan * 10000:
        return f"成交额不足: {float(avg_amt) / 10000.0:.1f}万 < {cfg.min_avg_amount_wan:.1f}万"
    return "未通过基础准入（综合条件不满足）"


def _signal_label(exit_sig: dict) -> str:
    return {
        "stop_loss": "触发结构止损",
        "distribution_warning": "触发Distribution派发警告",
        "upthrust_warning": "触发Upthrust/UTAD假突破派发警告",
    }.get(str(exit_sig.get("signal", "")).strip(), "触发风控硬剔除")
