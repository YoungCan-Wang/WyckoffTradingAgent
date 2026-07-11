"""Tail-buy report delivery and persistence workflow."""

from __future__ import annotations

import json
from datetime import datetime

from core.tail_buy.decision_semantics import tail_buy_execution_semantics
from core.tail_buy.reporting import build_tail_buy_markdown
from core.tail_buy.strategy import TailBuyCandidate
from integrations.supabase_market_signal import load_latest_market_signal_daily, load_market_signal_daily
from utils.feishu import send_feishu_notification, send_tail_buy_card
from utils.telegram import send_to_telegram
from workflows.tail_buy_runtime import TailBuyCandidateRun, TailBuyRuntimeConfig, env_flag
from workflows.tail_buy_utils import log_line, now_text, safe_float


def resolve_market_reminder(today_trade_date: str) -> str:
    from core.tail_buy.guardrails import TAIL_BLOCK_NEW_BUY_REGIMES

    row = load_market_signal_daily(today_trade_date) or load_latest_market_signal_daily()
    if not row:
        return "market_signal_daily 暂无可用记录（闸门未知，仍按候选 regime 拦截）"
    benchmark = str(row.get("benchmark_regime", "UNKNOWN") or "UNKNOWN").strip().upper()
    premarket = str(row.get("premarket_regime", "UNKNOWN") or "UNKNOWN").strip().upper()
    message = str(row.get("banner_message", "") or "").strip()
    blocked = benchmark in TAIL_BLOCK_NEW_BUY_REGIMES or premarket in TAIL_BLOCK_NEW_BUY_REGIMES
    gate = "禁止新开仓（尾盘不买新票）" if blocked else "允许新开仓（仍需 confirmed + BUY）"
    if message:
        return f"{benchmark}/{premarket} | {gate} | {message.replace(chr(10), ' ')}"
    return f"{benchmark}/{premarket} | {gate}"


def notify_tail_buy_non_trading_day(config: TailBuyRuntimeConfig) -> int:
    skip_msg = f"📅 今日 {config.started_at.strftime('%Y-%m-%d')} 非交易日，尾盘任务跳过"
    log_line(skip_msg, config.logs_path)
    if config.feishu_webhook:
        send_feishu_notification(config.feishu_webhook, "尾盘任务跳过", skip_msg)
    if config.tg_bot_token and config.tg_chat_id:
        send_to_telegram(skip_msg, tg_bot_token=config.tg_bot_token, tg_chat_id=config.tg_chat_id)
    return 0


def send_tail_buy_skip_notice(config: TailBuyRuntimeConfig, *, title: str, message: str) -> int:
    log_line(message, config.logs_path)
    if config.feishu_webhook:
        send_feishu_notification(config.feishu_webhook, title, message)
    if config.tg_bot_token and config.tg_chat_id:
        send_to_telegram(f"{title}\n\n{message}", tg_bot_token=config.tg_bot_token, tg_chat_id=config.tg_chat_id)
    return 0


def persist_tail_buy_results(
    merged: list[TailBuyCandidate], started_at: datetime, user_id: str, logs_path: str
) -> None:
    try:
        from integrations.local_db import init_db, save_tail_buy_results

        init_db()
        persistable = [tail_buy_persist_row(c, started_at) for c in merged if c.final_decision != "SKIP"]
        saved = save_tail_buy_results(persistable)
        log_line(f"已写入 {saved} 条尾盘结果到本地 SQLite", logs_path)
        buy_rows = [r for r in persistable if r["final_decision"] == "BUY"]
        if buy_rows:
            from integrations.supabase_tail_buy import save_tail_buy_to_supabase

            written = save_tail_buy_to_supabase(buy_rows, user_id=user_id)
            log_line(f"已写入 {written} 条 BUY 到 Supabase", logs_path)
    except Exception as exc:
        log_line(f"写入 SQLite 失败（不影响推送）: {exc}", logs_path)


def send_tail_buy_report(
    *,
    config: TailBuyRuntimeConfig,
    target_signal_date: str,
    market_reminder: str,
    candidate_source_desc: str,
    holdings_section: str,
    run_result: TailBuyCandidateRun,
    elapsed: float,
    report_mode: str = "intraday",
) -> tuple[bool, bool]:
    title = _tail_buy_report_title(config.started_at, report_mode)
    report = build_tail_buy_markdown(
        now_text=now_text(),
        target_signal_date=target_signal_date,
        market_reminder=market_reminder,
        candidates=run_result.merged,
        llm_total=run_result.llm_total,
        llm_success=run_result.llm_success,
        elapsed_seconds=elapsed,
        extra_sections=[holdings_section],
        extra_sections_first=True,
        candidate_source=candidate_source_desc,
        buy_only=report_mode != "post_close_review",
        data_fetched_at=run_result.data_fetched_at,
        report_mode=report_mode,
        policy_weights=run_result.policy_weights,
        policy_weight_meta=run_result.policy_weight_meta,
    )
    return send_tail_buy_notifications(
        feishu_webhook=config.feishu_webhook,
        tg_bot_token=config.tg_bot_token,
        tg_chat_id=config.tg_chat_id,
        title=title,
        report=report,
        logs_path=config.logs_path,
    )


def _tail_buy_report_title(started_at: datetime, report_mode: str) -> str:
    if report_mode == "post_close_review":
        return f"📋 盘后尾盘复核 {started_at.strftime('%Y-%m-%d')}"
    return f"⏰ Tail Buy {started_at.strftime('%Y-%m-%d')}"


def tail_buy_persist_row(candidate: TailBuyCandidate, started_at: datetime) -> dict:
    initial_price = safe_float(candidate.features.get("last_close"), 0.0)
    features = dict(candidate.features or {})
    features.update(
        {
            "candidate_theme": candidate.candidate_theme,
            "candidate_phase": candidate.candidate_phase,
            "candidate_role": candidate.candidate_role,
            "mainline_score": candidate.mainline_score,
            "theme_score": candidate.theme_score,
            "stock_role_score": candidate.stock_role_score,
        }
    )
    features.update(
        tail_buy_execution_semantics(candidate.final_decision, candidate.signal_type, features=candidate.features)
    )
    return {
        "code": candidate.code,
        "name": candidate.name,
        "run_date": started_at.strftime("%Y-%m-%d"),
        "signal_date": candidate.signal_date,
        "signal_type": candidate.signal_type,
        "status": candidate.status,
        "candidate_theme": candidate.candidate_theme,
        "candidate_phase": candidate.candidate_phase,
        "candidate_role": candidate.candidate_role,
        "mainline_score": candidate.mainline_score,
        "theme_score": candidate.theme_score,
        "stock_role_score": candidate.stock_role_score,
        "final_decision": candidate.final_decision,
        "rule_decision": candidate.rule_decision,
        "rule_score": candidate.rule_score,
        "priority_score": candidate.priority_score,
        "rule_reasons": json.dumps(candidate.rule_reasons, ensure_ascii=False),
        "llm_decision": candidate.llm_decision or "",
        "llm_reason": candidate.llm_reason,
        "llm_confidence": candidate.llm_confidence,
        "llm_model_used": candidate.llm_model_used,
        "initial_price": initial_price,
        "current_price": initial_price,
        "change_pct": 0.0,
        "price_updated_at": started_at.isoformat(),
        "last_close": candidate.features.get("last_close", 0.0),
        "vwap": candidate.features.get("vwap", 0.0),
        "dist_vwap_pct": candidate.features.get("dist_vwap_pct", 0.0),
        "close_pos": candidate.features.get("close_pos", 0.0),
        "day_ret_pct": candidate.features.get("day_ret_pct", 0.0),
        "last30_ret_pct": candidate.features.get("last30_ret_pct", 0.0),
        "last15_ret_pct": candidate.features.get("last15_ret_pct", 0.0),
        "tail30_volume_share": candidate.features.get("tail30_volume_share", 0.0),
        "drop_from_high_pct": candidate.features.get("drop_from_high_pct", 0.0),
        "fetch_error": candidate.fetch_error,
        "features_json": json.dumps(features, ensure_ascii=False, default=str),
    }


def send_tail_buy_notifications(
    *,
    feishu_webhook: str,
    tg_bot_token: str,
    tg_chat_id: str,
    title: str,
    report: str,
    logs_path: str | None = None,
) -> tuple[bool, bool]:
    feishu_ok = _send_feishu_tail_buy(feishu_webhook, title, report, logs_path)
    tg_ok = _send_tg_tail_buy(tg_bot_token, tg_chat_id, title, report, logs_path)
    return feishu_ok, tg_ok


def _send_feishu_tail_buy(feishu_webhook: str, title: str, report: str, logs_path: str | None) -> bool:
    if not feishu_webhook:
        log_line("FEISHU_WEBHOOK_URL 未配置", logs_path)
        return False
    try:
        if env_flag("FEISHU_TAIL_BUY_RICH_CARD", True):
            ok = bool(send_tail_buy_card(feishu_webhook, title, report))
            if ok:
                return True
            log_line("Tail Buy 富卡片发送失败，降级为文本卡片重试。", logs_path)
        return bool(send_feishu_notification(feishu_webhook, title, report))
    except Exception as exc:
        log_line(f"飞书推送异常: {exc}", logs_path)
        return False


def _send_tg_tail_buy(tg_bot_token: str, tg_chat_id: str, title: str, report: str, logs_path: str | None) -> bool:
    if not tg_bot_token or not tg_chat_id:
        log_line("Telegram 未完整配置，跳过 Telegram 推送", logs_path)
        return False
    try:
        return bool(send_to_telegram(f"{title}\n\n{report}", tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id))
    except Exception as exc:
        log_line(f"Telegram 推送异常: {exc}", logs_path)
        return False
