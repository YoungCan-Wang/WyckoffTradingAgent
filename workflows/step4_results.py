"""Step4 OMS result preparation and persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from integrations.supabase_portfolio import (
    cancel_trade_orders,
    save_ai_trade_orders,
    update_position_stops,
    upsert_daily_nav,
)
from utils.trading_clock import CN_TZ
from workflows.step4_models import ExecutionTicket, Step4InputContext, Step4RunOptions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step4ResultRecord:
    run_id: str
    ticket_rows: list[dict]


def prepare_step4_result_record(
    *,
    tickets: list[ExecutionTicket],
    state_signature: str,
) -> Step4ResultRecord:
    ticket_rows = build_step4_ticket_rows(tickets)
    log_step4_reject_audit(tickets)
    return Step4ResultRecord(_build_step4_run_id(state_signature), ticket_rows)


def save_step4_orders_and_nav(
    *,
    options: Step4RunOptions,
    context: Step4InputContext,
    run_id: str,
    rendered_market_view: str,
    tickets: list[ExecutionTicket],
    ticket_rows: list[dict],
) -> bool:
    if not _save_step4_trade_orders(options, context, run_id, rendered_market_view, ticket_rows):
        logger.error("AI 订单记录写入失败 | portfolio_id=%s", options.portfolio_id)
        return False
    _cancel_previous_trade_orders(options, context, run_id)
    stops_ok = update_step4_position_stops(options.portfolio_id, tickets)
    nav_ok = _save_step4_nav_snapshot(options, context)
    return stops_ok and nav_ok


def update_step4_position_stops(portfolio_id: str, tickets: list[ExecutionTicket]) -> bool:
    # NO_TRADE / 拒单不得改持仓状态；否则 T+1 拦截的 EXIT 仍会用模型止损覆写仓位。
    updates = [
        {"code": ticket.code, "stop_loss": ticket.effective_stop_loss}
        for ticket in tickets
        if ticket.status == "APPROVED" and ticket.is_holding and ticket.effective_stop_loss is not None
    ]
    if not updates:
        return True
    if update_position_stops(portfolio_id, updates):
        logger.info("已更新 %s 个持仓的止损价 | portfolio_id=%s", len(updates), portfolio_id)
        return True
    else:
        logger.error("持仓止损价更新失败 | portfolio_id=%s", portfolio_id)
        return False


def build_step4_ticket_rows(tickets: list[ExecutionTicket]) -> list[dict]:
    return [
        {
            "code": ticket.code,
            "name": ticket.name,
            "action": ticket.action,
            "status": ticket.status,
            "shares": ticket.shares,
            "price_hint": ticket.price_hint,
            "amount": ticket.amount,
            "stop_loss": ticket.stop_loss,
            "max_loss": ticket.max_loss,
            "drawdown_ratio": ticket.drawdown_ratio,
            "reason": _ticket_reason(ticket),
            "tape_condition": ticket.tape_condition,
            "invalidate_condition": ticket.invalidate_condition,
            "wyckoff_context": ticket.wyckoff_context,
        }
        for ticket in tickets
    ]


def _ticket_reason(ticket: ExecutionTicket) -> str:
    parts = [ticket.reason]
    if ticket.wyckoff_context:
        parts.append(f"context={ticket.wyckoff_context}")
    if ticket.audit:
        parts.append(f"audit={ticket.audit}")
    return " | ".join(part for part in parts if part).strip()


def log_step4_reject_audit(tickets: list[ExecutionTicket]) -> None:
    for ticket in tickets:
        if ticket.status != "APPROVED":
            logger.info(
                "[reject_audit] code=%s, action=%s, reason=%s, audit=%s, context=%s",
                ticket.code,
                ticket.action,
                ticket.reason,
                ticket.audit,
                ticket.wyckoff_context,
            )
    reject_cnt = sum(1 for ticket in tickets if ticket.status != "APPROVED")
    if reject_cnt:
        logger.info("[reject_audit] summary: rejected=%s, total=%s", reject_cnt, len(tickets))


def _build_step4_run_id(state_signature: str) -> str:
    run_id = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S") + "_" + str(uuid4())[:8]
    if state_signature:
        run_id += f"_sig{state_signature.lower()}"
    return run_id


def _save_step4_trade_orders(
    options: Step4RunOptions,
    context: Step4InputContext,
    run_id: str,
    rendered_market_view: str,
    ticket_rows: list[dict],
) -> bool:
    ok = save_ai_trade_orders(
        run_id=run_id,
        portfolio_id=options.portfolio_id,
        model=options.model,
        trade_date=context.trade_date,
        market_view=rendered_market_view,
        orders=ticket_rows,
    )
    if ok:
        logger.info(
            "已写入 AI 订单记录: run_id=%s, count=%s, portfolio_id=%s",
            run_id,
            len(ticket_rows),
            options.portfolio_id,
        )
    return bool(ok)


def _cancel_previous_trade_orders(options: Step4RunOptions, context: Step4InputContext, run_id: str) -> None:
    cancelled = cancel_trade_orders(
        portfolio_id=options.portfolio_id,
        trade_date=context.trade_date,
        exclude_run_id=run_id,
    )
    if cancelled:
        logger.info("已作废同日旧 AI 订单: cancelled=%s, portfolio_id=%s", cancelled, options.portfolio_id)


def _save_step4_nav_snapshot(options: Step4RunOptions, context: Step4InputContext) -> bool:
    """净值快照必须记账户当前的真实状态，不能记 OMS「假设你照单执行后」的模拟现金。

    工单未被执行时两者会持续背离：模拟现金把卖出所得算进来，残差算出的持仓市值就趋近 0，
    于是净值表显示满仓现金、实际却还拿着正在下跌的票，账户从此无法自证盈亏。
    """
    real_cash = float(context.portfolio.free_cash)
    positions_value = max(float(context.total_equity) - real_cash, 0.0)
    if upsert_daily_nav(
        portfolio_id=options.portfolio_id,
        trade_date=context.trade_date,
        free_cash=real_cash,
        total_equity=float(context.total_equity),
        positions_value=positions_value,
    ):
        logger.info("已写入 %s 日净值快照: %s", options.portfolio_id, context.trade_date)
        return True
    else:
        logger.warning("%s 日净值快照写入失败（已忽略）", options.portfolio_id)
        return False
