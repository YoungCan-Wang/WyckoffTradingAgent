"""
阶段 4：私人账户再平衡决策（OMS 重构版）
1) LLM 只输出结构化动作 JSON
2) Python 订单管理引擎负责仓位/手数/风险计算
3) 输出标准交易工单并推送 Telegram
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date

from core.execution_audit import StaleExit, find_unexecuted_exits, stop_breached_codes, unsellable_dates
from core.llm_docs import build_llm_doc_context
from integrations.fetch_a_share_csv import TradingWindow, resolve_trading_window
from integrations.supabase_portfolio import check_daily_run_exists, load_recent_trade_orders
from utils.telegram import send_to_telegram
from utils.trading_clock import resolve_end_calendar_day
from workflows.holding_diagnosis_core import fetch_holding_daily_frame
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
    DecisionItem,
    ExecutionTicket,
    PortfolioState,
    Step4InputContext,
    Step4OrderConfig,
    Step4RunOptions,
    Step4RuntimeConfig,
)
from workflows.step4_order_config import step4_order_config_from_env
from workflows.step4_payload import (
    extract_stock_codes,
)
from workflows.step4_payload import (
    prepare_step4_payload_context as _prepare_step4_payload_context,
)
from workflows.step4_portfolio import load_step4_portfolio_state
from workflows.step4_results import prepare_step4_result_record, rollback_step4_run, save_step4_orders_and_nav
from workflows.step4_runtime_config import step4_runtime_config_from_env
from workflows.step4_ticket import render_trade_ticket

logger = logging.getLogger(__name__)


def _run_stop_loss_only_fallback(
    options: Step4RunOptions,
    context: Step4InputContext,
    report_progress,
    status: str,
) -> tuple[bool, str]:
    """LLM 全部失败时，仍按系统规则出一张「只保护、不调仓」的工单。

    此前 LLM 一失败就 return，整条 OMS 随之 exit 1——2026-08-09/10 连续两天因
    「OpenAI 兼容接口返回内容为空」完全停摆，期间连持仓与止损状态都看不到。而实盘
    302 笔推荐里 57% 跌破 -8% 仍在持有（MAE 均值 -13.96%、最差 -59.2%），说明这段
    停摆期恰恰是最需要止损保护的时候。

    这里对每个持仓生成 HOLD 决策，不含任何 LLM 主观判断：买入一律不做，卖出只可能
    来自 WyckoffOrderEngine 的结构止损兜底（_process_hold 在跌破止损线时把 HOLD 降级
    为强制 EXIT）。即"模型不可用时不主动开仓，但止损照旧执行"。
    """
    positions = list(getattr(getattr(context, "portfolio", None), "positions", None) or [])
    if not positions:
        logger.error("Step4 模型不可用且无持仓，无可降级内容 | status=%s", status)
        return (False, status)

    logger.warning("Step4 模型不可用（%s），降级为「仅止损保护」工单，持仓 %d 只", status, len(positions))
    report_progress("LLM决策", "降级为仅止损保护", 0.6)
    decisions = [
        DecisionItem(
            code=str(pos.code),
            name=str(getattr(pos, "name", "") or pos.code),
            action="HOLD",
            entry_zone_min=None,
            entry_zone_max=None,
            stop_loss=getattr(pos, "stop_loss", None),
            trim_ratio=None,
            tape_condition="",
            invalidate_condition="",
            is_add_on=False,
            reason=f"模型不可用（{status}），系统降级：维持持仓，仅保留结构止损兜底",
            confidence=None,
        )
        for pos in positions
    ]
    tickets, free_cash_after = execute_step4_decisions(context, decisions, options.order_config)
    report = render_trade_ticket(
        market_view=f"⚠️ 决策模型不可用（{status}），本工单仅执行止损保护，不含调仓建议",
        total_equity=float(context.total_equity),
        free_cash_before=context.portfolio.free_cash,
        free_cash_after=free_cash_after,
        tickets=tickets,
        atr_period=options.runtime_config.atr_period,
        model_label=f"degraded:{status}",
    )
    _send_trade_ticket(report, options.tg_bot_token, options.tg_chat_id)
    forced = [t for t in tickets if t.action == "EXIT"]
    logger.warning("降级工单已发出：持仓 %d 只，其中强制止损 %d 只", len(tickets), len(forced))
    # 仍报失败，避免掩盖 LLM 故障；但工单与止损已执行。
    return (False, f"{status}_degraded")


def _step4_model_label(options) -> str:
    """provider:model，用于把决策模型写进工单。

    容忍缺字段：部分测试用 SimpleNamespace 构造精简 options，缺字段时返回空串
    让工单省略该行，而不是让一个展示用字段中断下单主流程。
    """
    provider = str(getattr(options, "provider", "") or "").strip()
    model = str(getattr(options, "model", "") or "").strip()
    if not provider and not model:
        return ""
    return f"{provider or '?'}:{model or '?'}"


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


def _step4_evidence_contract(trade_date: str) -> str:
    return (
        "[分析契约]\n"
        "analysis_mode=portfolio_rebalance\n"
        "capital_scope=account_only\n"
        "账户现金、权重和总权益仅代表当前证券账户，不代表用户全部可投资资产。\n"
        "先评价股票自身质量与风险，再评价账户内角色；不得只因本账户现金比例或集中度卖出。\n\n"
        "[证据可用性]\n"
        f"price_volume=available_as_of_{trade_date}\n"
        "portfolio=available_current_snapshot\n"
        "fundamentals=available_only_when_explicitly_in_input\n"
        "news=available_only_when_explicitly_in_input\n"
        "missing_evidence_policy=do_not_infer\n\n"
    )


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
    trade_date: str,
    order_config: Step4OrderConfig,
    ai_candidate_policy: str,
) -> str:
    doc_symbols = (
        [position.code for position in portfolio.positions]
        + list(candidate_codes)
        + extract_stock_codes(external_report or "")
    )
    llm_doc_context = build_llm_doc_context("step4", symbols=doc_symbols, as_of=date.fromisoformat(trade_date))
    msg = (
        benchmark_text
        + _step4_evidence_contract(trade_date)
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
        + "仅允许依据结构止损、Distribution 信号与量价破坏做减仓/清仓，不得因为持有天数到期而机械离场。\n"
        + "持仓硬止损只看本仓成本、buy_dt 之后的路径和该仓自己的 stop_loss；"
        + "建仓前的前高/箱体不是本仓硬止损。buy_dt 缺失时不得把全历史暴跌当成破位。\n\n"
        + "[持仓动作规则]\n"
        + "EXIT: 只在 HARD_RISK 或收盘确认的 CONFIRMED_BREAK 时使用。\n"
        + "TRIM: 只在 HARD_RISK 或收盘确认的 CONFIRMED_BREAK 时使用；普通走弱只能标 WARNING 并 HOLD。\n"
        + "HOLD: 默认动作。结构未破坏、止损未触发、无更强替代候选时必须继续持有。\n"
        + "PROBE/ATTACK加仓: 只允许已有持仓浮盈、止损已上移、且当前结构明显强于原买点时使用；禁止亏损补仓。\n"
        + "action_timing=WAIT/CLOSE_CONFIRM 时不得生成卖单；接近日内低点且未确认时优先 CLOSE_CONFIRM/ON_REBOUND。\n"
        + "新开仓: 输入候选已通过确定性准入；AI只能否决或保留，不能把候选升级为无条件买入。\n"
        + "外部新仓最多给 PROBE，禁止由AI升级为 ATTACK。\n\n"
        + (llm_doc_context + "\n\n" if llm_doc_context else "")
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


def _market_signal_context(trade_date: str) -> dict | None:
    row = _load_market_signal_for_trade_date(trade_date)
    if row:
        logger.info(
            "读取全局风控: trade_date=%s, benchmark=%s, premarket=%s",
            trade_date,
            row.get("benchmark_regime") or "-",
            row.get("premarket_regime") or "-",
        )
    else:
        logger.info("未读取到当日全局风控: trade_date=%s", trade_date)
    return row


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
    market_signal_row = _market_signal_context(trade_date)
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
        # 与 guardrail、OMS 用同一份禁买集合，避免 ALLOW 豁免在这一层被重新拦掉。
        max_new_buy_names=_parser_max_new_buy_names(
            market_regime,
            runtime_config.new_buy_limits,
            frozenset(order_config.buy_block_regimes),
        ),
        positions_payload=payloads.positions_payload,
        candidate_payload=payloads.candidate_payload,
        position_failures=payloads.position_failures,
        candidate_failures=payloads.candidate_failures,
        holdings_intraday_report=holdings_intraday_report,
        external_report=external_report,
        trade_date=trade_date,
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


def _unsellable_by_code(codes: Iterable[str]) -> dict[str, set[str]]:
    """各标的的一字跌停日。取数失败时按「可卖」处理，宁可多提醒也不要漏掉真实拖延。"""
    out: dict[str, set[str]] = {}
    for code in codes:
        df = fetch_holding_daily_frame(code)
        if df is None or df.empty:
            continue
        tail = df.tail(40)
        prev = tail["close"].shift(1)
        bars = [
            (str(row.date)[:10], float(p), float(row.high), float(row.low))
            for row, p in zip(tail.itertuples(), prev, strict=True)
            if p == p  # 首行 shift 出来的 NaN
        ]
        sealed = unsellable_dates(bars)
        if sealed:
            out[code] = sealed
    return out


def _audit_unexecuted_exits(portfolio_id: str, context: Step4InputContext) -> tuple[list[StaleExit], frozenset[str]]:
    """返回（告警用的全部拖延项，触发买入闸门的代码集）。

    两者刻意不同：告警把所有连续未落地的离场都摆出来，闸门只认「现价已跌破止损」的，
    没落袋的止盈不该冻结新仓。
    """
    positions = context.portfolio.positions
    held = [pos.code for pos in positions]
    stale = find_unexecuted_exits(load_recent_trade_orders(portfolio_id), held)
    if not stale:
        return ([], frozenset())

    stale = find_unexecuted_exits(
        load_recent_trade_orders(portfolio_id),
        held,
        unsellable_by_code=_unsellable_by_code(s.code for s in stale),
    )
    blocking = stop_breached_codes(
        stale,
        {pos.code: pos.stop_loss for pos in positions},
        context.latest_price_map,
    )
    if stale:
        logger.warning(
            "存在未执行的离场工单: %s；其中已跌破止损、冻结 ATTACK 的: %s",
            ", ".join(f"{s.code}×{s.days}日" for s in stale),
            ", ".join(sorted(blocking)) or "无",
        )
    return (stale, blocking)


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
        model_label=_step4_model_label(options),
    )
    persistence = save_step4_orders_and_nav(
        options=options,
        context=context,
        run_id=result_record.run_id,
        rendered_market_view=rendered_market_view,
        tickets=tickets,
        ticket_rows=result_record.ticket_rows,
    )
    if not persistence.ok:
        if persistence.orders_written and not rollback_step4_run(
            portfolio_id=options.portfolio_id,
            trade_date=context.trade_date,
            run_id=result_record.run_id,
            stop_rollback=persistence.stop_rollback,
        ):
            return False, "persistence_failed_rollback_failed"
        return False, "persistence_failed"
    if not _send_trade_ticket(report, options.tg_bot_token, options.tg_chat_id):
        logger.error(
            "交易工单通知失败，已保留落库订单且禁止重跑 OMS: run_id=%s, portfolio_id=%s",
            result_record.run_id,
            options.portfolio_id,
        )
        return False, "notification_failed_orders_preserved"
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
        return _run_stop_loss_only_fallback(options, context, report_progress, status)
    rendered_market_view = rendered_step4_market_view(context.system_market_view, decision_result.market_view)
    decisions = complete_step4_decisions(
        decision_result.decisions,
        context.portfolio,
        context.candidate_meta_map,
        context.market_regime,
        options.runtime_config,
        # 传 order_config 才能让裁剪配额与上面提示词里的 max_new_buy_names 同源。
        options.order_config,
    )
    backfill_step4_decision_market_data(
        decisions,
        context.window,
        context.latest_price_map,
        context.atr_map,
        options.runtime_config,
    )
    stale_exits, blocking_codes = _audit_unexecuted_exits(options.portfolio_id, context)
    tickets, free_cash_after = execute_step4_decisions(context, decisions, options.order_config, blocking_codes)
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
