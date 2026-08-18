"""分市场交易成本：A 股 / 港股 / 美股的费率结构不同，不能共用一套。

两处已知缺口（2026-08-17 复盘）：

1. ``core/trade_fill.py`` 对非人民币成交直接把手续费置零（``fee = ... if rate == 1.0
   else 0.0``）。原意是避免 A 股「最低佣金 5 元」被当成 5 美元摊进本币成本——这个顾虑
   是对的，但结果是港美成交**完全不计费**，已实现盈亏因此偏乐观。
2. ``core/trade_friction.py`` 的 ``round_trip_cost_pct`` 只有 A 股口径，信号层净收益
   对港美标的会低估成本。

费率结构的关键差异（这也是不能共用一套配置的原因）：

- **A 股**：佣金有**最低 5 元**门槛（小额交易的百分比成本被显著推高）；印花税 0.05%
  仅卖出单边；过户费 0.001% 双边。
- **港股**：印花税 0.1%（2023-11 起，**买卖双边**都征，与 A 股只收卖出侧不同）；
  交易征费 0.0027%、财汇局征费 0.00015%、交易所交易费 0.00565%，均双边；
  券商佣金常有最低收费（港币计）。
- **美股**：无印花税；卖出侧有 SEC 规费与 FINRA TAF；零佣金券商很常见，
  但 TAF 按**股数**计（每股 0.000166 美元、单笔上限 8.30 美元），与金额无关。

各档默认值取自公开费率表的常见档位，**不是从你的对账单实测出来的**。真实费率取决于
券商与账户等级，可用 env 覆盖。把它们显式化的意义在于让成本可见可调，而不是置零假装不存在。
"""

from __future__ import annotations

from dataclasses import dataclass

BUY = "buy"
SELL = "sell"

CN = "CN"
HK = "HK"
US = "US"


@dataclass(frozen=True)
class MarketFeeConfig:
    """单一市场的单边费率。比例项均按成交金额计，除 ``per_share_fee``。"""

    commission_rate: float
    min_commission: float
    # 印花税。``stamp_duty_both_sides`` 为 True 时买卖双边都征（港股），否则只在卖出侧（A 股）。
    stamp_duty_rate: float = 0.0
    stamp_duty_both_sides: bool = False
    # 其它按金额计的双边杂费合计（过户费、交易征费、交易所交易费等）。
    misc_rate_both_sides: float = 0.0
    # 只在卖出侧征收的按金额计规费（美股 SEC 规费）。
    sell_only_rate: float = 0.0
    # 按股数计的费用与其单笔上限（美股 FINRA TAF）。
    per_share_fee: float = 0.0
    per_share_fee_cap: float = 0.0


# A 股：与 core.cash_portfolio.CashPortfolioConfig 的默认值保持一致，避免两处漂移。
CN_FEES = MarketFeeConfig(
    commission_rate=0.0002,
    min_commission=5.0,
    stamp_duty_rate=0.0005,
    stamp_duty_both_sides=False,
    misc_rate_both_sides=0.00001,
)

# 港股：印花税双边 0.1% 是主要成本项，远高于 A 股的单边 0.05%。
HK_FEES = MarketFeeConfig(
    commission_rate=0.0003,
    min_commission=15.0,  # 港币
    stamp_duty_rate=0.001,
    stamp_duty_both_sides=True,
    misc_rate_both_sides=0.0000885,  # 交易征费 + 财汇局征费 + 交易所交易费
)

# 美股：默认零佣金，成本主要在卖出侧规费与按股数的 TAF。
US_FEES = MarketFeeConfig(
    commission_rate=0.0,
    min_commission=0.0,
    sell_only_rate=0.0000278,  # SEC 规费（费率逐年调整）
    per_share_fee=0.000166,  # FINRA TAF
    per_share_fee_cap=8.30,
)

_MARKET_FEES = {CN: CN_FEES, HK: HK_FEES, US: US_FEES}


def market_of(code: str) -> str:
    """从持仓代码判断市场。与 core.portfolio_valuation.portfolio_currency 同源。"""
    normalized = str(code or "").strip().upper()
    if normalized.endswith(".HK"):
        return HK
    if normalized.endswith(".US"):
        return US
    return CN


def fees_for_market(market: str) -> MarketFeeConfig:
    return _MARKET_FEES.get(str(market or "").strip().upper(), CN_FEES)


def single_side_cost(
    amount: float,
    *,
    side: str,
    market: str = CN,
    shares: float = 0.0,
    config: MarketFeeConfig | None = None,
) -> float:
    """单边交易成本，以**成交货币**计（港股得港币、美股得美元）。

    调用方负责换汇：把本币成本乘汇率再改人民币现金，不要先换汇再算费
    （最低佣金门槛是本币概念，换汇后判断会失真）。
    """
    gross = max(float(amount), 0.0)
    if gross <= 0:
        return 0.0
    fees = config or fees_for_market(market)
    cost = max(gross * fees.commission_rate, fees.min_commission)
    cost += gross * fees.misc_rate_both_sides
    if fees.stamp_duty_both_sides or side == SELL:
        cost += gross * fees.stamp_duty_rate
    if side == SELL:
        cost += gross * fees.sell_only_rate
        if fees.per_share_fee > 0:
            taf = max(float(shares), 0.0) * fees.per_share_fee
            cost += min(taf, fees.per_share_fee_cap) if fees.per_share_fee_cap > 0 else taf
    return cost


def round_trip_cost_pct(
    notional: float,
    *,
    market: str = CN,
    shares: float = 0.0,
    slippage_bps_per_side: float = 0.0,
) -> float:
    """一次完整买卖的成本占名义金额的百分比（含双边滑点）。"""
    gross = max(float(notional), 1.0)
    fees = single_side_cost(gross, side=BUY, market=market, shares=shares) + single_side_cost(
        gross, side=SELL, market=market, shares=shares
    )
    slippage = gross * (max(float(slippage_bps_per_side), 0.0) / 10_000.0) * 2.0
    return (fees + slippage) / gross * 100.0
