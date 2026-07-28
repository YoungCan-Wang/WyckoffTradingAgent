"""Cross-sectional composite scoring and tradability masks.

合成而不是单用某一项，是因为单因子收窄到可实现的持仓数（几十只）之后，分位级的 alpha 会被
个股特异波动淹没：BP 单独取前 40 只时跨窗 t 只有 0.39，四项合成后升到 2.7 以上。四项本质上是
同一个「便宜」信号的不同含噪观测，取秩后平均可以抵掉各自的噪声。

用秩而不是 z-score：PE/PB/PS 这类比率右偏且有负值（亏损、负净资产），少数极端值就能把
z-score 的均值拽跑，而秩对单调变换免疫。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.cn_boards import cn_board

# 分母为正才有「便宜」的含义：亏损股的 1/PE 为负会被排到最低分位，这是想要的行为，
# 但 PB<=0（资不抵债）在 A 股多为退市边缘，直接置空而不是当成极便宜。
VALUE_FACTORS = {
    "bp": ("pb", True),
    "ep_ttm": ("pe_ttm", False),
    "sp_ttm": ("ps_ttm", True),
    "dv_ttm": ("dv_ttm", False),
}
MIN_VALID_FACTORS = 3
BOARD_LIMIT_PCT = {"bse": 30.0, "chinext": 20.0, "star": 20.0}
ST_MAIN_LIMIT_PCT = 5.0
DEFAULT_LIMIT_PCT = 10.0
LIMIT_TOLERANCE = 0.005


def add_value_factors(panel: pd.DataFrame) -> pd.DataFrame:
    out = panel
    for name, (source, invert_only_positive) in VALUE_FACTORS.items():
        if name == source:
            continue
        raw = pd.to_numeric(out[source], errors="coerce")
        denominator = raw.where(raw > 0) if invert_only_positive else raw.replace(0.0, np.nan)
        out[name] = 1.0 / denominator
    return out


def value_composite(panel: pd.DataFrame, *, factors: tuple[str, ...] = tuple(VALUE_FACTORS)) -> pd.Series:
    """按日把各因子转成 [-0.5, 0.5] 的秩位再取均值；有效项不足则置空。"""
    ranks = pd.concat([panel.groupby("date")[name].rank(pct=True) - 0.5 for name in factors], axis=1)
    composite = ranks.mean(axis=1, skipna=True)
    return composite.where(ranks.notna().sum(axis=1) >= MIN_VALID_FACTORS)


def limit_pct_series(symbols: pd.Series, is_st: pd.Series) -> pd.Series:
    """`core.limit_move.limit_pct` 的向量化等价物；面板有千万行，逐行调用不可行。"""
    boards = symbols.map(lambda s: cn_board(f"{int(s):06d}"))
    pct = boards.map(BOARD_LIMIT_PCT).astype("float64")
    main = pct.isna()
    pct[main] = np.where(is_st.reindex(pct.index).fillna(False)[main], ST_MAIN_LIMIT_PCT, DEFAULT_LIMIT_PCT)
    return pct


def add_tradability(panel: pd.DataFrame) -> pd.DataFrame:
    """次日开盘能否成交。涨停开盘买不进，跌停开盘卖不出，无量视为停牌。"""
    out = panel
    limit = limit_pct_series(out["symbol"], out.get("is_st", pd.Series(False, index=out.index))) / 100.0
    gap = out["open"] / out["pre_close"] - 1.0
    has_volume = pd.to_numeric(out["vol"], errors="coerce").fillna(0.0) > 0
    out["can_buy"] = has_volume & (gap < limit - LIMIT_TOLERANCE)
    out["can_sell"] = has_volume & (gap > -limit + LIMIT_TOLERANCE)
    return out


def add_normalized_prices(panel: pd.DataFrame) -> pd.DataFrame:
    """后复权，但每只股票按其首个观测日归一，使价格量级贴近当年真实成交价。

    直接用 tushare 的 adj_factor 会把老股票的历史价放大数倍，最小佣金和一手门槛这些
    与绝对金额挂钩的约束就会失真。
    """
    out = panel
    factor = pd.to_numeric(out["adj_factor"], errors="coerce")
    base = factor.groupby(out["symbol"]).transform("first")
    scale = (factor / base).where(base > 0, 1.0).fillna(1.0)
    out["close_adj"] = out["close"] * scale
    out["open_adj"] = out["open"] * scale
    return out


def prepare_panel(
    panel: pd.DataFrame,
    st: pd.DataFrame | None = None,
    list_dates: pd.DataFrame | None = None,
) -> pd.DataFrame:
    out = panel.copy()
    if st is not None and not st.empty:
        out = out.merge(st, on=["date", "symbol"], how="left")
    if "is_st" not in out.columns:
        out["is_st"] = False
    out["is_st"] = out["is_st"].astype("boolean").fillna(False).astype(bool)
    if list_dates is not None and not list_dates.empty:
        out = out.merge(list_dates, on="symbol", how="left")
    out = add_value_factors(out)
    out = add_normalized_prices(out)
    out = add_tradability(out)
    out["score"] = value_composite(out)
    return out


def apply_universe_filters(
    panel: pd.DataFrame,
    *,
    exclude_st: bool,
    min_amount_thousand: float,
    min_listed_days: int,
) -> pd.DataFrame:
    """把不可投资的行的分数置空，而不是删行——净值仍需要它们的收盘价来估值已有持仓。"""
    out = panel
    eligible = out["score"].notna()
    if exclude_st:
        eligible &= ~out["is_st"]
    if min_amount_thousand > 0:
        eligible &= pd.to_numeric(out["amount"], errors="coerce").fillna(0.0) >= min_amount_thousand
    if min_listed_days > 0 and "list_date" in out.columns:
        listed_for = (out["date"] - out["list_date"]).dt.days
        eligible &= listed_for.fillna(-1) >= min_listed_days
    out["score"] = out["score"].where(eligible)
    return out
