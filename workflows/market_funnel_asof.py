"""As-of (历史某交易日) 重放美股/港股漏斗，用于回刷推荐表。

生产漏斗按 ``get_quotes`` 的实时快照排名并截断候选池，因此无法直接用于历史日期：
今天调用报价永远返回今天的价格与成交额，用它决定"某历史日谁进候选池"等于引入未来
信息。本模块改为纯历史口径：

- 候选池排名用**目标日当天**的历史成交额（``dollar_volume_series``，美股 amount 恒为 0
  时回退 close*volume），不使用实时报价；
- 每个标的的历史序列在目标日**截断**，L1~L4 只能看到当日及之前的数据；
- 名称走 universe meta，缺失时回退代码本身（美股 meta 的 name 目前为空，回刷出的
  名称会是代码，这是取数侧限制，调用方需知情）。

只做信号重建，不做收益归因；``current_price`` 由下游 performance 刷新任务负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from core.wyckoff_engine import dollar_volume_series, sort_by_date_if_needed
from workflows.market_funnel_runtime import RuntimeConfig


@dataclass(frozen=True)
class AsOfPool:
    """目标日的候选池与截断后的历史。"""

    as_of: date
    ranked: list[dict[str, Any]]
    df_map: dict[str, pd.DataFrame]


def truncate_history(df: pd.DataFrame | None, as_of: date) -> pd.DataFrame | None:
    """把历史裁到 as_of（含）为止；目标日无数据或行数不足时返回 None。"""
    if df is None or df.empty or "date" not in df.columns:
        return None
    work = sort_by_date_if_needed(df).copy()
    stamps = pd.to_datetime(work["date"], errors="coerce")
    work = work[stamps.dt.date <= as_of]
    if work.empty:
        return None
    last = pd.to_datetime(work["date"], errors="coerce").dt.date.iloc[-1]
    return work.reset_index(drop=True) if last == as_of else None


def build_asof_pool(
    df_map: dict[str, pd.DataFrame],
    name_map: dict[str, str],
    runtime: RuntimeConfig,
    as_of: date,
) -> AsOfPool:
    """用目标日历史成交额重建候选池，替代实时报价排名。"""
    rows: list[dict[str, Any]] = []
    truncated: dict[str, pd.DataFrame] = {}
    for symbol, raw in df_map.items():
        frame = truncate_history(raw, as_of)
        if frame is None or len(frame) < runtime.min_history_rows:
            continue
        row = _asof_rank_row(symbol, frame, name_map, runtime)
        if row is None:
            continue
        truncated[symbol] = frame
        rows.append(row)
    rows.sort(key=lambda item: (item["amount"], item["volume"]), reverse=True)
    ranked = rows[: runtime.max_symbols]
    kept = {str(item["symbol"]) for item in ranked}
    return AsOfPool(as_of=as_of, ranked=ranked, df_map={k: v for k, v in truncated.items() if k in kept})


def _asof_rank_row(
    symbol: str,
    frame: pd.DataFrame,
    name_map: dict[str, str],
    runtime: RuntimeConfig,
) -> dict[str, Any] | None:
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if close.empty:
        return None
    last_price = float(close.iloc[-1])
    if last_price <= 0 or last_price < runtime.min_quote_price:
        return None
    dollar_volume = dollar_volume_series(frame)
    amount = float(dollar_volume.iloc[-1]) if not dollar_volume.empty else 0.0
    if amount < runtime.min_quote_amount:
        return None
    volume = _numeric_column(frame, "volume")
    return {
        "symbol": symbol,
        "name": str(name_map.get(symbol) or symbol).strip() or symbol,
        "last_price": last_price,
        "amount": amount,
        "volume": float(volume.iloc[-1]) if not volume.empty else 0.0,
        "change_pct": _latest_change_pct(frame),
        "sector": "",
    }


def _numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").dropna()


def _latest_change_pct(frame: pd.DataFrame) -> float:
    pct = _numeric_column(frame, "pct_chg")
    if not pct.empty:
        return float(pct.iloc[-1])
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if len(close) < 2 or float(close.iloc[-2]) <= 0:
        return 0.0
    return (float(close.iloc[-1]) / float(close.iloc[-2]) - 1.0) * 100.0


__all__ = ["AsOfPool", "build_asof_pool", "truncate_history"]
