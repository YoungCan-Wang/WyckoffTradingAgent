"""影子账本飞书卡文案。只负责展示，不改账本或成交。"""

from __future__ import annotations

from datetime import date

from core.shadow_ledger import ShadowBook, ShadowPlan, ShadowPosition, ShadowSession


def render_shadow_card(session: ShadowSession, as_of: date) -> tuple[str, str]:
    title = f"📒 影子账本 / paper {as_of.isoformat()}"
    nav = session.nav
    lines = [
        "纸面对照账本，不是实盘。",
        f"**现金** {nav.get('cash', 0):,.2f}  **净值** {nav.get('equity', 0):,.2f}  "
        f"**市值** {nav.get('market_value', 0):,.2f}  **累计** {nav.get('pnl_total', 0):+,.2f}",
        "",
        "**今日成交**",
        *_fill_lines(session.fills),
        "",
        "**今夜计划（次日开盘）**",
        *_plan_lines(session.new_plans),
        "",
        "**持仓**",
        *_position_lines(session.book),
    ]
    return title, "\n".join(lines)


def format_open_net_pnl(shares: int, avg_cost: float, mark: float) -> str:
    """开仓未实现净收益：市值 − 含买费成本；百分比分母为 shares * avg_cost。

    ``avg_cost`` 来自 ``apply_fill`` 的 ``cost_price``，买入均价已摊入买侧佣金/
    印花税/过户费。开仓尚未付卖费，故此数是盯市浮盈，不是已实现清仓净额。
    """
    cost_basis = shares * avg_cost
    pnl = shares * mark - cost_basis
    if cost_basis <= 0:
        return f"净收益 {pnl:+,.2f}"
    return f"净收益 {pnl:+,.2f} ({pnl / cost_basis * 100.0:+.1f}%)"


def _fill_lines(fills: list[ShadowPlan]) -> list[str]:
    done = [plan for plan in fills if plan.status == "filled"]
    if not done:
        return ["今日无成交"]
    return [
        f"  {plan.action} {plan.code} {plan.name}  {plan.qty}股 @ {plan.entry_price:.2f}  {plan.fill_reason}"
        for plan in done
    ]


def _plan_lines(plans: list[ShadowPlan]) -> list[str]:
    if not plans:
        return ["今夜无新计划"]
    return [
        f"  {plan.action} {plan.code} {plan.name}  约{plan.shares_hint}股  参考{plan.suggested_price}  {plan.reason}"
        for plan in plans
    ]


def _position_lines(book: ShadowBook) -> list[str]:
    rows = [pos for pos in book.positions.values() if pos.shares > 0]
    if not rows:
        return ["空仓"]
    return [_format_position_line(pos) for pos in rows]


def _format_position_line(pos: ShadowPosition) -> str:
    mark = _mark_price(pos)
    return (
        f"  {pos.code} {pos.name}  {pos.shares}股  成本{pos.avg_cost:.2f}  现价{mark:.2f}  "
        f"{format_open_net_pnl(pos.shares, pos.avg_cost, mark)}  可卖{pos.sellable_shares}"
    )


def _mark_price(pos: ShadowPosition) -> float:
    if pos.last_mark is not None and pos.last_mark > 0:
        return float(pos.last_mark)
    return float(pos.avg_cost)
