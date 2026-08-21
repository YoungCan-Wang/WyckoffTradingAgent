"""信号级交易成本：把毛收益折成扣费后的净收益。

为什么需要这个：``signal_outcomes.return_pct`` 一直是纯毛收益
（``(exit_close - entry) / entry``，见 core/signal_lifecycle.py），不含佣金、印花税、
过户费与滑点。于是所有基于它的结论都偏乐观——2026-08 复盘时 T+5 超额 −4.04%、
T+20 −16.23% 都是**未扣成本**的数字，真实更差。

``core.cash_portfolio`` 里已有一套完整且正确的 A 股费率（佣金含最低收费、卖出单边
印花税、双边过户费），但它只服务组合回测，信号层没有接上。本模块复用同一套费率，
避免两处各写一份而漂移。

滑点单独给默认值：A 股主板双边合计约 0.1%~0.3%，小盘与低价股显著更高。这里取
0.05% 单边作保守默认。**这不是实测值**，真实滑点取决于委托方式与流动性；把它显式化
的意义在于让成本可见、可调，而不是假装为零。

按 core 层的架构约束（tests/test_architecture_boundaries.py），本模块不读环境变量：
滑点由运行时通过 ``configure_slippage`` 注入，见 utils.runtime_friction。
"""

from __future__ import annotations

from core.cash_portfolio import CashPortfolioConfig, calc_trade_cost
from core.market_trade_cost import CN, market_of, single_side_cost

DEFAULT_SLIPPAGE_BPS_PER_SIDE = 5.0
# 每笔按此名义金额估算佣金最低收费的影响。A 股佣金常有 5 元门槛，
# 金额越小、费率占比越高；用一个代表性单笔金额把它折成百分比。
DEFAULT_NOTIONAL_YUAN = 20_000.0


_slippage_bps_per_side = DEFAULT_SLIPPAGE_BPS_PER_SIDE


def configure_slippage(bps_per_side: float | None) -> None:
    """由运行时（utils.env / 脚本入口）注入滑点，core 不直接读环境变量。

    传 None 或非法值时回落到默认值，保证 core 始终有可用配置。
    """
    global _slippage_bps_per_side
    try:
        _slippage_bps_per_side = max(0.0, float(bps_per_side))
    except (TypeError, ValueError):
        _slippage_bps_per_side = DEFAULT_SLIPPAGE_BPS_PER_SIDE


def slippage_bps_per_side() -> float:
    return _slippage_bps_per_side


def round_trip_cost_pct(
    notional: float = DEFAULT_NOTIONAL_YUAN,
    config: CashPortfolioConfig | None = None,
    *,
    code: str = "",
) -> float:
    """一次完整买卖的成本占名义金额的百分比。

    ``code`` 决定市场：给出港美代码时按对应费率算，缺省按 A 股。此前只有 A 股口径，
    港美标的的净收益会低估成本——港股双边印花税 0.1% 使其往返成本约为 A 股的 3.6 倍。
    显式传入的 ``config`` 仍优先，且只适用于 A 股（它是人民币费率覆盖）。
    """
    gross = max(float(notional), 1.0)
    market = market_of(code) if code else CN
    if market == CN and config is not None:
        fees = calc_trade_cost(gross, config, side="buy") + calc_trade_cost(gross, config, side="sell")
    else:
        fees = single_side_cost(gross, side="buy", market=market) + single_side_cost(gross, side="sell", market=market)
    slippage = gross * (slippage_bps_per_side() / 10_000.0) * 2.0
    return (fees + slippage) / gross * 100.0


def net_return_pct(
    gross_return_pct: float | None,
    *,
    notional: float = DEFAULT_NOTIONAL_YUAN,
    config: CashPortfolioConfig | None = None,
    code: str = "",
) -> float | None:
    """毛收益 -> 扣除双边成本后的净收益（百分数）。``code`` 用于选市场费率。"""
    if gross_return_pct is None:
        return None
    return round(float(gross_return_pct) - round_trip_cost_pct(notional, config, code=code), 6)


def friction_breakdown(
    notional: float = DEFAULT_NOTIONAL_YUAN,
    config: CashPortfolioConfig | None = None,
) -> dict[str, float]:
    """成本构成，用于报告与文档，避免「总成本」变成不可追溯的魔法数字。"""
    cfg = config or CashPortfolioConfig()
    gross = max(float(notional), 1.0)
    buy_fee = calc_trade_cost(gross, cfg, side="buy")
    sell_fee = calc_trade_cost(gross, cfg, side="sell")
    slippage = gross * (slippage_bps_per_side() / 10_000.0) * 2.0
    return {
        "notional_yuan": gross,
        "buy_fee_pct": round(buy_fee / gross * 100.0, 6),
        "sell_fee_pct": round(sell_fee / gross * 100.0, 6),
        "slippage_pct": round(slippage / gross * 100.0, 6),
        "round_trip_pct": round((buy_fee + sell_fee + slippage) / gross * 100.0, 6),
        "slippage_bps_per_side": slippage_bps_per_side(),
    }
