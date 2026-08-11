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


@dataclass(frozen=True)
class Step4PersistenceResult:
    ok: bool
    orders_written: bool = False
    stop_rollback: tuple[dict, ...] = ()


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
) -> Step4PersistenceResult:
    if not _save_step4_trade_orders(options, context, run_id, rendered_market_view, ticket_rows):
        logger.error("AI 订单记录写入失败 | portfolio_id=%s", options.portfolio_id)
        return Step4PersistenceResult(False)
    stop_rollback = _stop_rollback_updates(context, tickets)
    stops_ok = update_step4_position_stops(options.portfolio_id, tickets)
    nav_ok = _save_step4_nav_snapshot(options, context)
    if not (stops_ok and nav_ok):
        return Step4PersistenceResult(False, orders_written=True, stop_rollback=stop_rollback)
    try:
        _cancel_previous_trade_orders(options, context, run_id)
    except Exception:
        logger.exception("同日旧 AI 订单作废失败 | portfolio_id=%s", options.portfolio_id)
        return Step4PersistenceResult(False, orders_written=True, stop_rollback=stop_rollback)
    return Step4PersistenceResult(True, orders_written=True)


def rollback_step4_run(
    *,
    portfolio_id: str,
    trade_date: str,
    run_id: str,
    stop_rollback: tuple[dict, ...] | list[dict] = (),
) -> bool:
    """作废本轮已写入的工单并恢复止损；回滚失败必须对调用方可见。"""
    try:
        cancelled = cancel_trade_orders(
            portfolio_id=portfolio_id,
            trade_date=trade_date,
            only_run_id=run_id,
            raise_on_error=True,
        )
    except Exception:
        logger.exception(
            "Step4 本轮订单回滚失败: run_id=%s, portfolio_id=%s, trade_date=%s",
            run_id,
            portfolio_id,
            trade_date,
        )
        return False
    if stop_rollback and not update_position_stops(portfolio_id, list(stop_rollback)):
        logger.error(
            "Step4 止损回滚失败: count=%s, run_id=%s, portfolio_id=%s",
            len(stop_rollback),
            run_id,
            portfolio_id,
        )
        return False
    logger.error(
        "Step4 持久化失败，已作废本轮订单: cancelled=%s, stops_restored=%s, run_id=%s, portfolio_id=%s, trade_date=%s",
        cancelled,
        len(stop_rollback),
        run_id,
        portfolio_id,
        trade_date,
    )
    return True


def update_step4_position_stops(portfolio_id: str, tickets: list[ExecutionTicket]) -> bool:
    updates = [
        {"code": ticket.code, "stop_loss": ticket.effective_stop_loss}
        for ticket in tickets
        if _should_persist_stop(ticket)
    ]
    if not updates:
        return True
    if update_position_stops(portfolio_id, updates):
        logger.info("已更新 %s 个持仓的止损价 | portfolio_id=%s", len(updates), portfolio_id)
        return True
    else:
        logger.error("持仓止损价更新失败 | portfolio_id=%s", portfolio_id)
        return False


#: 会落库止损价的动作。PROBE/ATTACK（买入建仓）必须包含在内。
#:
#: 2026-08-10 修复：此前只有 {HOLD, TRIM} 落库，于是新建仓的止损价被算出来却不写库
#: （_raise_entry_stop_from_atr 明确为 PROBE/ATTACK 算了 ATR 止损）。要等该股下一次
#: 被判为 HOLD/TRIM 才补上，而 Step4 每天只处理模型给出决策的标的——实测 196 个持仓
#: 里 189 个（96%）stop_loss 为空。
#:
#: 后果是 _process_hold 的强制止损兜底形同虚设：它的判据是
#: `ctx.effective_stop_loss and ctx.current_price <= ctx.effective_stop_loss`，
#: stop 为 None 时直接短路。实盘 302 笔推荐里 57% 跌破 -8% 仍在持有，MAE 均值
#: -13.96%、最差 -59.2%（对照回测因有 -8% 强制止损，MAE 被截断在 -6~-8.6%）。
_STOP_PERSIST_ACTIONS = frozenset({"HOLD", "TRIM", "PROBE", "ATTACK"})


def _should_persist_stop(ticket: ExecutionTicket) -> bool:
    """未成交卖单不改持仓；倒挂参考价也不能成为下一轮保护止损。"""
    stop = ticket.effective_stop_loss
    price = ticket.price_hint
    # 买入建仓时 is_holding 为 False（首次买入 held_shares=0），不能作为落库前提，
    # 否则新仓的止损价依旧写不进去——这正是要修的场景。加仓与持有走 is_holding 分支。
    is_entry = ticket.action in {"PROBE", "ATTACK"}
    return bool(
        ticket.status == "APPROVED"
        and (ticket.is_holding or is_entry)
        and ticket.action in _STOP_PERSIST_ACTIONS
        and stop is not None
        and price is not None
        and 0 < stop < price
    )


def _stop_rollback_updates(context: Step4InputContext, tickets: list[ExecutionTicket]) -> tuple[dict, ...]:
    """Snapshot pre-mutation stop losses so failed runs can restore portfolio state."""
    positions = getattr(getattr(context, "portfolio", None), "positions", None) or []
    previous = {str(pos.code): pos.stop_loss for pos in positions if getattr(pos, "code", None)}
    return tuple(
        {"code": ticket.code, "stop_loss": previous.get(ticket.code)}
        for ticket in tickets
        if ticket.status == "APPROVED" and ticket.is_holding and ticket.effective_stop_loss is not None
    )


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
        raise_on_error=True,
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
