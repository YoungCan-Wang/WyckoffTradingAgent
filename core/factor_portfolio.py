"""Cross-sectional long-only portfolio simulation with realistic A-share frictions.

与 `core/backtest_run.py` 的逐信号台账是两种问题：那边是「某只票出现形态后单独买卖一笔」，
这边是「每隔 K 个交易日把全市场重排一次，持有分数最高的 N 只」。持仓集合是一个整体，换手由
排名变动决定，摩擦要按真实的一手门槛和最低佣金算——每笔 2000 元时 5 元最低佣金就是 0.25%
的单边成本，用一个笼统的百分比会低估。

缓冲式再平衡：已持仓跌出 top N 不立即卖，掉出 top N×buffer 才卖。价值分在相邻期高度相关，
边界附近的名次抖动会制造大量无意义换手。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from core.cash_portfolio import CashPortfolioConfig, calc_trade_cost

CANDIDATE_DEPTH = 4


@dataclass(frozen=True)
class FactorPortfolioConfig:
    top_n: int = 50
    rebalance_days: int = 10
    buffer_mult: float = 2.0
    slippage_bps: float = 10.0
    costs: CashPortfolioConfig = field(default_factory=CashPortfolioConfig)

    def __post_init__(self) -> None:
        if self.top_n < 1:
            raise ValueError("top_n 必须 >= 1")
        if self.rebalance_days < 1:
            raise ValueError("rebalance_days 必须 >= 1")
        if self.buffer_mult < 1.0:
            raise ValueError("buffer_mult 必须 >= 1（小于 1 等于提前卖出，无意义）")


@dataclass(frozen=True)
class PanelMatrices:
    dates: pd.DatetimeIndex
    symbols: np.ndarray
    close: np.ndarray
    close_held: np.ndarray
    open_: np.ndarray
    score: np.ndarray
    can_buy: np.ndarray
    can_sell: np.ndarray
    last_valid: np.ndarray


@dataclass
class _State:
    cash: float
    shares: dict[int, int] = field(default_factory=dict)
    pending_sell: set[int] = field(default_factory=set)
    pending_buy: list[int] = field(default_factory=list)


def build_matrices(panel: pd.DataFrame) -> PanelMatrices:
    wide = {
        name: panel.pivot(index="date", columns="symbol", values=column)
        for name, column in (
            ("close", "close_adj"),
            ("open_", "open_adj"),
            ("score", "score"),
            ("can_buy", "can_buy"),
            ("can_sell", "can_sell"),
        )
    }
    close = wide["close"]
    alive = close.notna().to_numpy()
    # 最后一个有价日；晚于它就是永久停止交易（退市），中间的空洞才是停牌。
    last_valid = np.where(alive.any(axis=0), alive.shape[0] - 1 - alive[::-1].argmax(axis=0), -1)
    return PanelMatrices(
        dates=pd.DatetimeIndex(close.index),
        symbols=close.columns.to_numpy(),
        close=close.to_numpy(dtype="float64"),
        # 停牌期间按「截至当日的最后一个成交价」估值。直接取该股在整个面板里的最后价格会把
        # 复牌后的价格提前用上，是未来函数。
        close_held=close.ffill().to_numpy(dtype="float64"),
        open_=wide["open_"].to_numpy(dtype="float64"),
        score=wide["score"].to_numpy(dtype="float64"),
        can_buy=wide["can_buy"].eq(True).to_numpy(),
        can_sell=wide["can_sell"].eq(True).to_numpy(),
        last_valid=last_valid,
    )


def _slip(price: float, bps: float, *, side: str) -> float:
    return price * (1.0 + bps / 10_000.0) if side == "buy" else price * (1.0 - bps / 10_000.0)


def _sell(state: _State, col: int, price: float, cfg: FactorPortfolioConfig) -> dict | None:
    shares = state.shares.pop(col, 0)
    if shares <= 0 or not np.isfinite(price) or price <= 0:
        return None
    fill = _slip(price, cfg.slippage_bps, side="sell")
    gross = shares * fill
    cost = calc_trade_cost(gross, cfg.costs, side="sell")
    state.cash += gross - cost
    return {"col": col, "side": "sell", "shares": shares, "price": fill, "cost": cost}


def _buy(state: _State, col: int, price: float, budget: float, cfg: FactorPortfolioConfig) -> dict | None:
    if not np.isfinite(price) or price <= 0:
        return None
    fill = _slip(price, cfg.slippage_bps, side="buy")
    lot = max(int(cfg.costs.lot_size), 1)
    usable = min(state.cash, budget)
    shares = int(usable // (fill * lot)) * lot
    while shares > 0 and shares * fill + calc_trade_cost(shares * fill, cfg.costs, side="buy") > usable:
        shares -= lot
    if shares <= 0:
        return None
    gross = shares * fill
    cost = calc_trade_cost(gross, cfg.costs, side="buy")
    state.cash -= gross + cost
    state.shares[col] = state.shares.get(col, 0) + shares
    return {"col": col, "side": "buy", "shares": shares, "price": fill, "cost": cost}


def _liquidate_delisted(state: _State, mat: PanelMatrices, i: int, cfg: FactorPortfolioConfig) -> list[dict]:
    """持仓股在 i 之前就再没有行情了：按最后一个成交价结算，不留在净值里当幽灵仓位。"""
    fills = []
    for col in [c for c in state.shares if mat.last_valid[c] < i]:
        fill = _sell(state, col, mat.close[mat.last_valid[col], col], cfg)
        if fill is not None:
            fills.append({**fill, "reason": "delisted"})
        state.shares.pop(col, None)
    return fills


def _execute(state: _State, mat: PanelMatrices, i: int, cfg: FactorPortfolioConfig) -> list[dict]:
    fills = []
    for col in sorted(state.pending_sell):
        if col in state.shares and mat.can_sell[i, col]:
            fill = _sell(state, col, mat.open_[i, col], cfg)
            if fill is not None:
                fills.append({**fill, "reason": "rebalance"})
            state.pending_sell.discard(col)
    # 预算按「剩余现金 / 剩余空位」滚动：一手制留下的零头会被后面的标的吸收，买不起的标的
    # 也不会白占一格，而是继续沿排名往下补。固定 equity/top_n 且不补位时，一手价高于预算的
    # 标的会被静默跳过，等于按股价做了一次隐性筛选，实测能凭空造出 8pct 的假 alpha。
    for col in state.pending_buy:
        slots_left = cfg.top_n - len(state.shares)
        if slots_left <= 0:
            break
        if col in state.shares or not mat.can_buy[i, col]:
            continue
        fill = _buy(state, col, mat.open_[i, col], state.cash / slots_left, cfg)
        if fill is not None:
            fills.append({**fill, "reason": "rebalance"})
    state.pending_buy = []
    state.pending_sell = {c for c in state.pending_sell if c in state.shares}
    return fills


def _plan(state: _State, mat: PanelMatrices, i: int, cfg: FactorPortfolioConfig) -> None:
    scores = mat.score[i]
    ranked = np.argsort(-np.where(np.isnan(scores), -np.inf, scores), kind="stable")
    ranked = ranked[np.isfinite(scores[ranked])]
    keep_pool = set(ranked[: int(cfg.top_n * cfg.buffer_mult)].tolist())
    state.pending_sell = {c for c in state.shares if c not in keep_pool}
    slots = cfg.top_n - (len(state.shares) - len(state.pending_sell))
    held = set(state.shares)
    # 候选给到空位数的若干倍：涨停买不进、一手价超预算都会消耗候选，不给深度就填不满仓位。
    depth = slots * CANDIDATE_DEPTH
    state.pending_buy = [c for c in ranked if c not in held][:depth] if slots > 0 else []


def run_factor_backtest(panel: pd.DataFrame, cfg: FactorPortfolioConfig) -> dict:
    mat = build_matrices(panel)
    state = _State(cash=float(cfg.costs.initial_cash))
    nav, positions, cash, trades = [], [], [], []
    for i in range(len(mat.dates)):
        trades.extend({**f, "date": mat.dates[i]} for f in _liquidate_delisted(state, mat, i, cfg))
        if state.pending_sell or state.pending_buy:
            trades.extend({**f, "date": mat.dates[i]} for f in _execute(state, mat, i, cfg))
        if i % cfg.rebalance_days == 0 and i + 1 < len(mat.dates):
            _plan(state, mat, i, cfg)
        nav.append(_equity(state, mat, i))
        positions.append(len(state.shares))
        cash.append(state.cash)
    return {
        "nav": pd.Series(nav, index=mat.dates, name="nav"),
        "positions": pd.Series(positions, index=mat.dates, name="positions"),
        "cash": pd.Series(cash, index=mat.dates, name="cash"),
        "trades": _trades_frame(trades, mat),
    }


def _equity(state: _State, mat: PanelMatrices, i: int) -> float:
    total = state.cash
    for col, shares in state.shares.items():
        total += shares * mat.close_held[i, col]
    return float(total)


def _trades_frame(trades: list[dict], mat: PanelMatrices) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["date", "symbol", "side", "shares", "price", "cost", "reason"])
    out = pd.DataFrame(trades)
    out["symbol"] = mat.symbols[out["col"].to_numpy()]
    return out.drop(columns=["col"])[["date", "symbol", "side", "shares", "price", "cost", "reason"]]


def equal_weight_benchmark(panel: pd.DataFrame) -> pd.Series:
    """在同一可投资池里每日等权再平衡的收益复利。

    这条线是常规的「市场」参照，但不能直接拿来算策略 alpha：它每天把权重摊回等权，趋势下跌
    时等于每天加仓正在下跌的票，与组合的 K 日持有不可比。随机打分在这条基准上能刷出接近 9pct
    的假 alpha，全部来自再平衡频率差异。算 alpha 请用 `holding_period_benchmark`。
    """
    close = panel.pivot(index="date", columns="symbol", values="close_adj")
    eligible = panel.pivot(index="date", columns="symbol", values="score").notna()
    daily = close.pct_change(fill_method=None)
    daily = daily.where(eligible.shift(1).eq(True) & close.notna() & close.shift(1).notna())
    return (1.0 + daily.mean(axis=1).fillna(0.0)).cumprod().rename("benchmark")


def _ffill_rows(block: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """按列前向填充停牌造成的空洞，首行缺失用 seed 兜底。"""
    out = block.copy()
    out[0] = np.where(np.isfinite(out[0]), out[0], seed)
    rows = np.where(np.isfinite(out), np.arange(out.shape[0])[:, None], 0)
    np.maximum.accumulate(rows, axis=0, out=rows)
    return out[rows, np.arange(out.shape[1])]


def holding_period_benchmark(mat: PanelMatrices, rebalance_days: int) -> pd.Series:
    """与组合同机制的等权基线：同样的调仓日、同样次日开盘换手、等权持有整个可投资池。

    与组合的差别只剩「选了哪些票」，两者之差才是选股本身的贡献。用每日再平衡的等权线去算
    alpha 会凭空多出近 9pct，那是再平衡频率差异，不是选股能力。
    """
    last = len(mat.dates) - 1
    boundaries = list(range(0, last, rebalance_days))
    nav = np.ones(len(mat.dates))
    level = 1.0
    for start, nxt in zip(boundaries, boundaries[1:] + [last], strict=False):
        entry_idx, exit_idx = start + 1, min(nxt + 1, last)
        entry = mat.open_[entry_idx]
        valid = np.isfinite(mat.score[start]) & mat.can_buy[entry_idx] & np.isfinite(entry) & (entry > 0)
        if not valid.any() or exit_idx <= entry_idx:
            nav[entry_idx : exit_idx + 1] = level
            continue
        base = entry[valid]
        path = _ffill_rows(mat.close[entry_idx:exit_idx, valid], base) / base
        nav[entry_idx:exit_idx] = level * path.mean(axis=1)
        exit_px = np.where(np.isfinite(mat.open_[exit_idx, valid]), mat.open_[exit_idx, valid], path[-1] * base)
        level *= float(np.mean(exit_px / base))
        nav[exit_idx] = level
    nav[: boundaries[0] + 1] = 1.0
    return pd.Series(nav, index=mat.dates, name="benchmark")


def sweep_configs(base: FactorPortfolioConfig, top_ns: list[int], rebalances: list[int]) -> list[FactorPortfolioConfig]:
    return [replace(base, top_n=n, rebalance_days=k) for n in top_ns for k in rebalances]
