"""把一笔真实成交折算成新的持仓与现金。

持仓表由人工维护，OMS 只发建议、不写回股数，所以只要成交没被录入，系统看到的仓位
就一直是旧的：止损会对着已经卖掉的票反复触发，净值也会长期偏离真实账户。这个模块
提供成交回填的纯计算部分，I/O 留给调用方，便于单测覆盖边界。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.cash_portfolio import CashPortfolioConfig, calc_trade_cost

BUY = "buy"
SELL = "sell"


@dataclass(frozen=True)
class Fill:
    code: str
    side: str
    shares: int
    price: float
    trade_date: str
    name: str = ""

    def __post_init__(self) -> None:
        if self.side not in (BUY, SELL):
            raise ValueError(f"side 只能是 {BUY} 或 {SELL}: {self.side}")
        if self.shares <= 0:
            raise ValueError(f"成交股数必须为正: {self.shares}")
        if self.price <= 0:
            raise ValueError(f"成交价必须为正: {self.price}")


@dataclass(frozen=True)
class Holding:
    """回填视角下的持仓，只保留与算账相关的字段。"""

    code: str
    name: str
    shares: int
    cost_price: float
    buy_dt: str


@dataclass(frozen=True)
class FillResult:
    holding: Holding | None
    cash: float
    fee: float
    realized_pnl: float | None
    note: str


def apply_fill(
    holding: Holding | None,
    cash: float,
    fill: Fill,
    config: CashPortfolioConfig | None = None,
) -> FillResult:
    """返回成交后的持仓与现金；卖出时一并给出已实现盈亏（含双边费用）。"""
    cfg = config or CashPortfolioConfig()
    gross = fill.shares * fill.price
    fee = calc_trade_cost(gross, cfg, side=fill.side)
    if fill.side == BUY:
        return _apply_buy(holding, cash, fill, gross, fee)
    return _apply_sell(holding, cash, fill, gross, fee)


def _apply_buy(holding: Holding | None, cash: float, fill: Fill, gross: float, fee: float) -> FillResult:
    outlay = gross + fee
    if outlay > cash + 1e-6:
        raise ValueError(f"现金不足：需要 {outlay:,.2f}，当前 {cash:,.2f}")
    prev_shares = holding.shares if holding else 0
    # 买入均价含手续费，卖出时算出的已实现盈亏才是到手的钱。
    prev_cost = (holding.cost_price * prev_shares) if holding else 0.0
    shares = prev_shares + fill.shares
    merged = Holding(
        code=fill.code,
        name=fill.name or (holding.name if holding else ""),
        shares=shares,
        cost_price=(prev_cost + outlay) / shares,
        buy_dt=fill.trade_date,
    )
    note = f"{fill.code} 买入 {fill.shares} 股 @ {fill.price:.3f}，费用 {fee:.2f}，持仓 {prev_shares} → {shares}"
    return FillResult(merged, cash - outlay, fee, None, note)


def _apply_sell(holding: Holding | None, cash: float, fill: Fill, gross: float, fee: float) -> FillResult:
    if holding is None or holding.shares <= 0:
        raise ValueError(f"{fill.code} 无持仓可卖")
    if fill.shares > holding.shares:
        raise ValueError(f"{fill.code} 卖出 {fill.shares} 股超过持仓 {holding.shares} 股")
    proceeds = gross - fee
    remaining = holding.shares - fill.shares
    realized = proceeds - holding.cost_price * fill.shares
    left = (
        None
        if remaining == 0
        else Holding(
            code=holding.code,
            name=holding.name,
            shares=remaining,
            cost_price=holding.cost_price,
            buy_dt=holding.buy_dt,
        )
    )
    tail = "已清仓" if remaining == 0 else f"剩余 {remaining} 股"
    note = f"{fill.code} 卖出 {fill.shares} 股 @ {fill.price:.3f}，费用 {fee:.2f}，已实现 {realized:+,.2f}，{tail}"
    return FillResult(left, cash + proceeds, fee, realized, note)
