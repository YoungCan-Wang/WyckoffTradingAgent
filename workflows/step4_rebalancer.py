"""
阶段 4：私人账户再平衡决策（OMS 重构版）
1) LLM 只输出结构化动作 JSON
2) Python 订单管理引擎负责仓位/手数/风险计算
3) 输出标准交易工单并推送 Telegram
"""

from __future__ import annotations

import logging
from datetime import date

from core.execution_audit import StaleExit, find_unexecuted_exits
from integrations.fetch_a_share_csv import TradingWindow, resolve_trading_window
from integrations.supabase_portfolio import check_daily_run_exists, load_recent_trade_orders
from utils.telegram import send_to_telegram
from utils.trading_clock import resolve_end_calendar_day
from workflows.step4_decision_parser import (
    max_new_buy_names as _parser_max_new_buy_names,
)
from workflows.step4_decisions import (
    backfill_step4_decision_market_data,
    complete_step4_decisions,
    execute_step4_decisions,
    rendered_step4_market_view,
)
from workflows.step4_llm import call_step4_decision_model
from workflows.step4_market import (
    build_market_guardrail as _build_market_guardrail,
)
from workflows.step4_market import (
    load_market_signal_for_trade_date as _load_market_signal_for_trade_date,
)
from workflows.step4_models import (
    ExecutionTicket,
    PortfolioState,
    Step4InputContext,
    Step4OrderConfig,
    Step4RunOptions,
    Step4RuntimeConfig,
)
from workflows.step4_order_config import step4_order_config_from_env
from workflows.step4_payload import (
    prepare_step4_payload_context as _prepare_step4_payload_context,
)
from workflows.step4_portfolio import load_step4_portfolio_state
from workflows.step4_results import prepare_step4_result_record, save_step4_orders_and_nav
from workflows.step4_runtime_config import step4_runtime_config_from_env
from workflows.step4_ticket import render_trade_ticket

logger = logging.getLogger(__name__)


def _resolve_step4_trade_context(runtime_config: Step4RuntimeConfig) -> tuple[date, TradingWindow, str]:
    end_day = resolve_end_calendar_day()
    window = resolve_trading_window(end_calendar_day=end_day, trading_days=runtime_config.trading_days)
    return end_day, window, window.end_trade_date.isoformat()


def _has_telegram_channel(tg_bot_token: str, tg_chat_id: str) -> bool:
    return bool(str(tg_bot_token or "").strip() and str(tg_chat_id or "").strip())


def _send_trade_ticket(report: str, tg_bot_token: str, tg_chat_id: str) -> bool:
    if _has_telegram_channel(tg_bot_token, tg_chat_id):
        return bool(
            send_to_telegram(
                report,
                tg_bot_token=tg_bot_token,
                tg_chat_id=tg_chat_id,
            )
        )
    logger.info("tg_bot_token/tg_chat_id 未配置，跳过 Step4 Telegram 发送")
    return False


def _build_user_message(
    *,
    benchmark_text: str,
    portfolio,
    total_equity: float,
    candidate_codes: list[str],
    allowed_codes: set[str],
    max_new_buy_names: int,
    positions_payload: str,
    candidate_payload: str,
    position_failures: list[str],
    candidate_failures: list[str],
    holdings_intraday_report: str,
    external_report: str,
    order_config: Step4OrderConfig,
    ai_candidate_policy: str,
) -> str:
    msg = (
        benchmark_text
        + "[账户状态]\n"
        + f"free_cash={portfolio.free_cash:.2f}\n"
        + f"total_equity={float(total_equity):.2f}\n"
        + f"position_count={len(portfolio.positions)}\n"
        + f"candidate_count={len(candidate_codes)}\n"
        + f"allowed_codes={','.join(sorted(allowed_codes))}\n\n"
        + "[组合决策约束]\n"
        + f"max_new_buy_names={max_new_buy_names}\n"
        + f"ai_candidate_policy={ai_candidate_policy}\n"
        + "external_candidates_are_optional=true\n"
        + "omit_rejected_candidates_from_decisions=true\n"
        + "prefer_cash_over_marginal_candidates=true\n"
        + "all_existing_positions_must_have_action=true\n\n"
        + "[系统硬规则]\n"
        + f"buy_stop_mode={order_config.buy_stop_mode}, buy_stop_pct={order_config.buy_hard_stop_pct:.1f}\n"
        + "仅允许依据结构止损、Distribution 信号与量价破坏做减仓/清仓，不得因为持有天数到期而机械离场。\n\n"
        + "[持仓动作规则]\n"
        + "EXIT: 只在跌破有效止损、出现明确派发/破位、或风控一票否决时使用。\n"
        + "TRIM: 只在逼近止损、放量跌破关键位、上涨后出现派发/滞涨时使用；不能只因为浮亏或持有天数而减仓。\n"
        + "HOLD: 默认动作。结构未破坏、止损未触发、无更强替代候选时必须继续持有。\n"
        + "PROBE/ATTACK加仓: 只允许已有持仓浮盈、止损已上移、且当前结构明显强于原买点时使用；禁止亏损补仓。\n"
        + "新开仓: 输入候选已通过确定性准入；AI只能否决或保留，不能把候选升级为无条件买入。\n"
        + "外部新仓最多给 PROBE，禁止由AI升级为 ATTACK。\n\n"
        + "[内部持仓量价切片]\n"
        + (positions_payload if positions_payload else "当前无持仓，仅现金。")
        + "\n\n[漏斗候选量价切片]\n"
        + (candidate_payload if candidate_payload else "无")
    )
    data_notes: list[str] = []
    data_notes.extend(position_failures)
    data_notes.extend(candidate_failures)
    if data_notes:
        msg += "\n\n[数据注意]\n" + "\n".join(f"- {x}" for x in data_notes)
    if holdings_intraday_report and holdings_intraday_report.strip():
        msg += "\n\n[持仓分钟级诊断]\n" + holdings_intraday_report.strip()
    if (not candidate_payload) and external_report and external_report.strip():
        msg += "\n\n[Step3参考摘要-仅在候选切片缺失时启用]\n" + external_report.strip()
    return msg


def _prepare_step4_input_context(
    *,
    portfolio: PortfolioState,
    state_signature: str,
    window: TradingWindow,
    trade_date: str,
    benchmark_context: dict | None,
    external_report: str,
    candidate_meta: list[dict] | None,
    holdings_intraday_report: str,
    runtime_config: Step4RuntimeConfig,
    order_config: Step4OrderConfig,
) -> Step4InputContext:
    payloads = _prepare_step4_payload_context(
        portfolio,
        window,
        external_report,
        candidate_meta,
        atr_period=runtime_config.atr_period,
        max_workers=runtime_config.max_workers,
        enforce_target_trade_date=runtime_config.enforce_target_trade_date,
        max_external_report_candidates=runtime_config.max_external_report_candidates,
    )
    market_signal_row = _load_market_signal_for_trade_date(trade_date)
    if market_signal_row:
        logger.info(
            "读取全局风控: trade_date=%s, benchmark=%s, premarket=%s",
            trade_date,
            market_signal_row.get("benchmark_regime") or "-",
            market_signal_row.get("premarket_regime") or "-",
        )
    else:
        logger.info("未读取到当日全局风控: trade_date=%s", trade_date)
    market_regime, benchmark_text, system_market_view = _build_market_guardrail(
        trade_date=trade_date,
        benchmark_context=benchmark_context,
        market_signal_row=market_signal_row,
        buy_block_regimes=set(order_config.buy_block_regimes),
    )
    user_message = _build_user_message(
        benchmark_text=benchmark_text,
        portfolio=portfolio,
        total_equity=payloads.total_equity,
        candidate_codes=payloads.candidate_codes,
        allowed_codes=payloads.allowed_codes,
        max_new_buy_names=_parser_max_new_buy_names(market_regime, runtime_config.new_buy_limits),
        positions_payload=payloads.positions_payload,
        candidate_payload=payloads.candidate_payload,
        position_failures=payloads.position_failures,
        candidate_failures=payloads.candidate_failures,
        holdings_intraday_report=holdings_intraday_report,
        external_report=external_report,
        order_config=order_config,
        ai_candidate_policy=runtime_config.ai_candidate_policy,
    )
    return Step4InputContext(
        portfolio=portfolio,
        state_signature=state_signature,
        window=window,
        trade_date=trade_date,
        total_equity=payloads.total_equity,
        latest_price_map=payloads.latest_price_map,
        atr_map=payloads.atr_map,
        allowed_codes=payloads.allowed_codes,
        candidate_meta_map=payloads.candidate_meta_map,
        name_map=payloads.name_map,
        market_regime=market_regime,
        system_market_view=system_market_view,
        user_message=user_message,
    )


def _send_and_persist_step4_results(
    *,
    options: Step4RunOptions,
    context: Step4InputContext,
    decisions: list[object],
    tickets: list[ExecutionTicket],
    free_cash_after: float,
    rendered_market_view: str,
    stale_exits: list[StaleExit],
    report_progress,
) -> tuple[bool, str]:
    result_record = prepare_step4_result_record(
        tickets=tickets,
        state_signature=context.state_signature,
    )
    report = render_trade_ticket(
        market_view=rendered_market_view,
        total_equity=float(context.total_equity),
        free_cash_before=context.portfolio.free_cash,
        free_cash_after=free_cash_after,
        tickets=tickets,
        atr_period=options.runtime_config.atr_period,
        stale_exits=stale_exits,
    )
    persisted = save_step4_orders_and_nav(
        options=options,
        context=context,
        run_id=result_record.run_id,
        rendered_market_view=rendered_market_view,
        tickets=tickets,
        ticket_rows=result_record.ticket_rows,
    )
    if not persisted:
        return False, "persistence_failed"
    if not _send_trade_ticket(report, options.tg_bot_token, options.tg_chat_id):
        return False, "notification_failed"
    logger.info(
        "交易工单发送成功: decisions=%s, tickets=%s, model=%s, portfolio_id=%s",
        len(decisions),
        len(tickets),
        options.model,
        options.portfolio_id,
    )
    report_progress("决策完成", f"订单={len(tickets)}条", 1.0)
    return True, "ok"


def _build_step4_run_options(
    *,
    provider: str,
    model: str,
    api_key: str,
    llm_base_url: str,
    portfolio_id: str,
    tg_bot_token: str,
    tg_chat_id: str,
    runtime_config: Step4RuntimeConfig,
    order_config: Step4OrderConfig,
) -> Step4RunOptions:
    return Step4RunOptions(
        provider=provider,
        model=model,
        api_key=api_key,
        llm_base_url=llm_base_url,
        portfolio_id=portfolio_id,
        tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id,
        runtime_config=runtime_config,
        order_config=order_config,
    )


def _load_step4_portfolio(portfolio_id: str) -> tuple[PortfolioState | None, str, str]:
    try:
        return load_step4_portfolio_state(portfolio_id)
    except Exception as e:
        logger.error("持仓读取失败: %s", e, exc_info=True)
        return None, "", ""


def _resolve_step4_run_window(
    portfolio_id: str,
    state_signature: str,
    runtime_config: Step4RuntimeConfig,
) -> tuple[TradingWindow | None, str, str | None]:
    end_day, window, trade_date = _resolve_step4_trade_context(runtime_config)
    if trade_date != end_day.isoformat():
        logger.info("trade_date 使用最近交易日: calendar_day=%s, trade_date=%s", end_day.isoformat(), trade_date)
    if check_daily_run_exists(portfolio_id, trade_date, state_signature=state_signature):
        logger.info("幂等性检查: %s %s 当前持仓快照已运行过，跳过。", portfolio_id, trade_date)
        return None, trade_date, "skipped_idempotency"
    return window, trade_date, None


def _run_step4_decision_flow(
    *,
    options: Step4RunOptions,
    context: Step4InputContext,
    report_progress,
) -> tuple[bool, str]:
    ok, status, decision_result = call_step4_decision_model(options, context, report_progress)
    if not ok or decision_result is None:
        return (ok, status)
    rendered_market_view = rendered_step4_market_view(context.system_market_view, decision_result.market_view)
    decisions = complete_step4_decisions(
        decision_result.decisions,
        context.portfolio,
        context.candidate_meta_map,
        context.market_regime,
        options.runtime_config,
    )
    backfill_step4_decision_market_data(
        decisions,
        context.window,
        context.latest_price_map,
        context.atr_map,
        options.runtime_config,
    )
    stale_exits = find_unexecuted_exits(
        load_recent_trade_orders(options.portfolio_id),
        [pos.code for pos in context.portfolio.positions],
    )
    if stale_exits:
        logger.warning("存在未执行的离场工单: %s", ", ".join(f"{s.code}×{s.days}日" for s in stale_exits))
    tickets, free_cash_after = execute_step4_decisions(
        context,
        decisions,
        options.order_config,
        frozenset(s.code for s in stale_exits),
    )
    return _send_and_persist_step4_results(
        options=options,
        context=context,
        decisions=decisions,
        tickets=tickets,
        free_cash_after=free_cash_after,
        rendered_market_view=rendered_market_view,
        stale_exits=stale_exits,
        report_progress=report_progress,
    )


def run(
    external_report: str,
    benchmark_context: dict | None,
    api_key: str,
    model: str,
    *,
    provider: str = "gemini",
    llm_base_url: str = "",
    candidate_meta: list[dict] | None = None,
    portfolio_id: str,
    tg_bot_token: str,
    tg_chat_id: str,
    holdings_intraday_report: str = "",
) -> tuple[bool, str]:
    if not api_key or not api_key.strip():
        return (False, "missing_api_key")
    if not portfolio_id:
        return (True, "skipped_invalid_portfolio")
    runtime_config = step4_runtime_config_from_env()
    order_config = step4_order_config_from_env()
    options = _build_step4_run_options(
        provider=provider,
        model=model,
        api_key=api_key,
        llm_base_url=llm_base_url,
        portfolio_id=portfolio_id,
        tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id,
        runtime_config=runtime_config,
        order_config=order_config,
    )

    portfolio, portfolio_source, state_signature = _load_step4_portfolio(portfolio_id)
    if portfolio is None:
        return (True, "skipped_invalid_portfolio")
    logger.info(
        "持仓来源: %s | portfolio_id=%s | state_sig=%s",
        portfolio_source,
        portfolio_id,
        state_signature or "-",
    )
    from utils.progress import report_progress

    report_progress("持仓决策", f"来源: {portfolio_source}", 0.1)

    if not _has_telegram_channel(tg_bot_token, tg_chat_id):
        logger.info("TG 未配置，跳过 Step4 推送")
        return (True, "skipped_notify_unconfigured")

    window, trade_date, skip_reason = _resolve_step4_run_window(portfolio_id, state_signature, options.runtime_config)
    if skip_reason or window is None:
        return (True, skip_reason or "skipped_invalid_window")

    context = _prepare_step4_input_context(
        portfolio=portfolio,
        state_signature=state_signature,
        window=window,
        trade_date=trade_date,
        benchmark_context=benchmark_context,
        external_report=external_report,
        candidate_meta=candidate_meta,
        holdings_intraday_report=holdings_intraday_report,
        runtime_config=options.runtime_config,
        order_config=options.order_config,
    )
    return _run_step4_decision_flow(
        options=options,
        context=context,
        report_progress=report_progress,
    )
