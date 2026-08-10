"""Step4 OMS trade ticket rendering."""

from __future__ import annotations

import re
from datetime import datetime

from core.execution_audit import StaleExit, render_stale_exit_alert
from core.execution_playbook import oms_playbook_lines
from utils.trading_clock import CN_TZ
from workflows.step4_models import ExecutionTicket


def render_trade_ticket(
    market_view: str,
    total_equity: float,
    free_cash_before: float,
    free_cash_after: float,
    tickets: list[ExecutionTicket],
    *,
    atr_period: int,
    stale_exits: list[StaleExit] | None = None,
    model_label: str = "",
) -> str:
    now_str = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    sells = [t for t in tickets if t.status == "APPROVED" and t.action in {"EXIT", "TRIM"}]
    holds = [t for t in tickets if t.status == "APPROVED" and t.action == "HOLD" and t.is_holding]
    approved_buy = [t for t in tickets if t.status == "APPROVED" and t.action in {"PROBE", "ATTACK"}]
    blocked = [t for t in tickets if t.status != "APPROVED"]

    lines = [
        "🚨 Alpha-OMS 交易执行工单",
        f"📅 日期：{now_str} | 净权益：{total_equity:.2f} | 当前可用现金：{free_cash_before:.2f}",
    ]
    if market_view:
        lines.append(f"📌 市场视图：{market_view}")
    # 拖延告警排在操作清单之前：读到一半就该知道昨天的单子还没落地。
    lines.extend(render_stale_exit_alert(stale_exits or []))
    lines.append("")
    lines.extend(oms_playbook_lines())
    lines.extend(_render_sell_ticket_lines(sells, atr_period=atr_period))
    lines.extend(_render_hold_ticket_lines(holds, atr_period=atr_period))
    lines.extend(_render_buy_ticket_lines(approved_buy, atr_period=atr_period))
    lines.extend(_render_blocked_ticket_lines(blocked))
    lines.append(f"💰 若全部工单成交后的预计可用现金：{free_cash_after:.2f}")
    if model_label:
        # Step4 的模型选型直接决定这张工单的内容（决策经 WyckoffOrderEngine 变成订单），
        # 因此必须与工单同屏可见，而不是只留在 CI 日志里。
        lines.append(f"🤖 决策模型：{model_label}")
    return "\n".join(lines)


def _ticket_first_sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "-"
    parts = re.split(r"[。；;\n]+", text, maxsplit=1)
    return parts[0].strip() if parts and parts[0].strip() else text


def _fmt_ticket_stop(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _append_ticket_context(lines: list[str], ticket: ExecutionTicket) -> None:
    if ticket.wyckoff_context:
        lines.append(f"  结构：{ticket.wyckoff_context}")


def _buy_entry_line(ticket: ExecutionTicket) -> str:
    if ticket.entry_zone_min is None or ticket.entry_zone_max is None:
        return "  买入：区间缺失，禁止执行"
    return f"  买入：明日允许区间 {ticket.entry_zone_min:.2f}–{ticket.entry_zone_max:.2f} 元；开盘价不在区间内则放弃"


def _render_sell_ticket_lines(sells: list[ExecutionTicket], *, atr_period: int) -> list[str]:
    lines = [f"🟥 [卖出动作 SELL] ({len(sells)})"]
    if not sells:
        return lines + ["- 无"]
    for ticket in sells:
        lines.append(f"- 🟥 {ticket.action} | {ticket.code} {ticket.name}")
        lines.append(
            f"  执行：{ticket.shares} 股 | 回笼：{ticket.amount:.2f} 元 | 止损：{_fmt_ticket_stop(ticket.stop_loss)}"
        )
        if ticket.atr14 is not None:
            lines.append(f"  风控：ATR{atr_period}={ticket.atr14:.3f} | 滑点={ticket.slippage_bps * 100:.2f}%")
        _append_ticket_context(lines, ticket)
        lines.append(f"  触发：{_ticket_first_sentence(ticket.tape_condition)}")
        lines.append(f"  失效：{_ticket_first_sentence(ticket.invalidate_condition)}")
        lines.append(f"  理由：{_ticket_first_sentence(ticket.reason)}")
        lines.append("")
    return lines


def _render_hold_ticket_lines(holds: list[ExecutionTicket], *, atr_period: int) -> list[str]:
    lines = [f"🟨 [持有动作 HOLD] ({len(holds)})"]
    if not holds:
        return lines + ["- 无", ""]
    for ticket in holds:
        lines.append(f"- 🟨 HOLD | {ticket.code} {ticket.name} | 止损：{_fmt_ticket_stop(ticket.stop_loss)}")
        if ticket.atr14 is not None:
            lines.append(
                f"  风控：ATR{atr_period}={ticket.atr14:.3f} | 动态止损={_fmt_ticket_stop(ticket.effective_stop_loss)}"
            )
        _append_ticket_context(lines, ticket)
        lines.append(f"  观察：{_ticket_first_sentence(ticket.reason)}")
        lines.append(f"  触发：{_ticket_first_sentence(ticket.tape_condition)}")
        lines.append(f"  失效：{_ticket_first_sentence(ticket.invalidate_condition)}")
        lines.append("")
    lines.append("")
    return lines


def _render_buy_ticket_lines(approved_buy: list[ExecutionTicket], *, atr_period: int) -> list[str]:
    lines = [f"🟩 [买入动作 BUY - APPROVED] ({len(approved_buy)})"]
    if not approved_buy:
        return lines + ["- 无", ""]
    for ticket in approved_buy:
        lines.append(f"- 🟩 {ticket.action} | {ticket.code} {ticket.name}")
        lines.append(f"  下单：{ticket.shares} 股 | 占用：{ticket.amount:.2f} 元")
        lines.append(_buy_entry_line(ticket))
        if ticket.chase_profile:
            lines.append(f"  分层：{ticket.chase_profile}")
        _append_ticket_context(lines, ticket)
        lines.append(
            f"  风险：止损 {_fmt_ticket_stop(ticket.stop_loss)} | 最大回撤 {ticket.max_loss:.2f} 元 "
            f"({ticket.drawdown_ratio * 100:.2f}%) | 滑点={ticket.slippage_bps * 100:.2f}%"
        )
        if ticket.atr14 is not None:
            lines.append(f"  ATR：ATR{atr_period}={ticket.atr14:.3f}")
        if ticket.tape_condition:
            lines.append(f"  确认：{_ticket_first_sentence(ticket.tape_condition)}")
        if ticket.invalidate_condition:
            lines.append(f"  熔断：{_ticket_first_sentence(ticket.invalidate_condition)}")
        if ticket.reason:
            lines.append(f"  理由：{_ticket_first_sentence(ticket.reason)}")
        lines.append("")
    lines.append("")
    return lines


def _render_blocked_ticket_lines(blocked: list[ExecutionTicket]) -> list[str]:
    lines = [f"⬛ [风控拒单 NO_TRADE] ({len(blocked)})"]
    if not blocked:
        return lines + ["- 无", ""]
    for ticket in blocked:
        lines.append(f"- ⬛ NO_TRADE | {ticket.code} {ticket.name} | 原动作：{ticket.action}")
        lines.append(f"  原因：{_ticket_first_sentence(ticket.reason)}")
        _append_ticket_context(lines, ticket)
        if ticket.audit:
            lines.append(f"  审计：{_ticket_first_sentence(ticket.audit)}")
        lines.append("")
    lines.append("")
    return lines
