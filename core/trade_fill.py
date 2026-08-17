"""把一笔真实成交折算成新的持仓与现金。

持仓表由人工维护，OMS 只发建议、不写回股数，所以只要成交没被录入，系统看到的仓位
就一直是旧的：止损会对着已经卖掉的票反复触发，净值也会长期偏离真实账户。这个模块
提供成交回填的纯计算部分，I/O 留给调用方，便于单测覆盖边界。

成本价按标的本币计（与估值一致）；`free_cash` 是人民币，外币成交必须乘 `fx_to_cny`
后才改现金，否则会把美元/港元报价直接当成人民币扣款。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.buy_dt import parse_buy_dt
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
    *,
    fx_to_cny: float = 1.0,
) -> FillResult:
    """返回成交后的持仓与现金；卖出时一并给出已实现盈亏（含双边费用）。

    ``fx_to_cny`` 是 1 单位报价币可兑换的人民币。A 股为 1.0；港美必须传入正汇率，
    缺汇率时拒绝算账，避免把外币名义金额写入人民币 ``free_cash``。
    """
    rate = float(fx_to_cny)
    if rate <= 0:
        raise ValueError("缺少有效汇率，无法把成交金额折成人民币现金")
    cfg = config or CashPortfolioConfig()
    gross = fill.shares * fill.price
    # A 股费用模型（最低佣金/印花税/过户费）只适用于人民币成交；港美暂不计费，
    # 避免把「最低 5 元」误当成 5 美元从本币成本里摊进去。
    fee = calc_trade_cost(gross, cfg, side=fill.side) if rate == 1.0 else 0.0
    if fill.side == BUY:
        return _apply_buy(holding, cash, fill, gross, fee, rate)
    return _apply_sell(holding, cash, fill, gross, fee, rate)


def _apply_buy(
    holding: Holding | None,
    cash: float,
    fill: Fill,
    gross: float,
    fee: float,
    fx_to_cny: float,
) -> FillResult:
    outlay_native = gross + fee
    outlay_cny = outlay_native * fx_to_cny
    if outlay_cny > cash + 1e-6:
        raise ValueError(f"现金不足：需要 {outlay_cny:,.2f}，当前 {cash:,.2f}")
    prev_shares = holding.shares if holding else 0
    # 买入均价含手续费（本币），卖出时算出的已实现盈亏才是到手的钱。
    prev_cost = (holding.cost_price * prev_shares) if holding else 0.0
    shares = prev_shares + fill.shares
    # T+1 以最近一次买入为准；乱序回填更早成交日不得把 buy_dt 往回拨，否则当日加仓会被误判可卖。
    prev_buy_dt = holding.buy_dt if holding else ""
    merged = Holding(
        code=fill.code,
        name=fill.name or (holding.name if holding else ""),
        shares=shares,
        cost_price=(prev_cost + outlay_native) / shares,
        buy_dt=_later_buy_dt(prev_buy_dt, fill.trade_date),
    )
    fx_note = f"，汇率 {fx_to_cny:.6f}" if fx_to_cny != 1.0 else ""
    note = (
        f"{fill.code} 买入 {fill.shares} 股 @ {fill.price:.3f}，费用 {fee:.2f}{fx_note}，持仓 {prev_shares} → {shares}"
    )
    return FillResult(merged, cash - outlay_cny, fee * fx_to_cny, None, note)


def _later_buy_dt(existing: str, incoming: str) -> str:
    """取较晚的建仓日；成交日缺失时返回空串，交由写入层保留库内原值。

    返回空串而非回填 ``existing``：``update_position`` 的契约是「空 buy_dt 保留原日期」，
    靠上层剪掉空值实现。若在此处回写同一个值，payload 就会显式带上 buy_dt，
    绕过那条边界约束（tests/agents/test_portfolio_write_boundary.py 会拦）。
    功能上两者等价，但不写字段更保守——不碰库里的值。
    """
    incoming_text = str(incoming or "").strip()
    existing_text = str(existing or "").strip()
    new = parse_buy_dt(incoming_text)
    if new is None:
        # 成交日缺失或不可解析：不改动建仓日。
        return ""
    old = parse_buy_dt(existing_text)
    if old is None:
        return incoming_text
    return incoming_text if new.date() >= old.date() else existing_text


def _apply_sell(
    holding: Holding | None,
    cash: float,
    fill: Fill,
    gross: float,
    fee: float,
    fx_to_cny: float,
) -> FillResult:
    if holding is None or holding.shares <= 0:
        raise ValueError(f"{fill.code} 无持仓可卖")
    if fill.shares > holding.shares:
        raise ValueError(f"{fill.code} 卖出 {fill.shares} 股超过持仓 {holding.shares} 股")
    proceeds_native = gross - fee
    remaining = holding.shares - fill.shares
    realized_cny = (proceeds_native - holding.cost_price * fill.shares) * fx_to_cny
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
    fx_note = f"，汇率 {fx_to_cny:.6f}" if fx_to_cny != 1.0 else ""
    note = (
        f"{fill.code} 卖出 {fill.shares} 股 @ {fill.price:.3f}，"
        f"费用 {fee:.2f}{fx_note}，已实现 {realized_cny:+,.2f}，{tail}"
    )
    return FillResult(left, cash + proceeds_native * fx_to_cny, fee * fx_to_cny, realized_cny, note)
