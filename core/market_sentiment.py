"""全市场逐日情绪计数：涨停 / 跌停 / 触板未封 / 连板高度 / 涨跌家数。

游资口中的"赚钱效应 / 亏钱效应 / 情绪周期"在日线上能落到的就是这几个数。本模块只计数，
不定义状态——状态阈值属于研究协议，放在 workflows.backtest_regime_timing 并预登记。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from core.backtest_execution import LIMIT_TOUCH_TOLERANCE
from core.cn_boards import cn_board
from core.limit_move import limit_pct

# 创业板 2020-08-24 起由 10% 改为 20%；limit_pct 只认当前规则，计数要按当日规则。
_CHINEXT_20PCT_START = date(2020, 8, 24)
SENTIMENT_COLUMNS = (
    "date",
    "symbols",
    "advancers",
    "decliners",
    "up_limit",
    "down_limit",
    "touched_up",
    "broke_up",
    "max_streak",
)


def build_market_sentiment(all_df_map: dict[str, pd.DataFrame], name_map: dict[str, str]) -> pd.DataFrame:
    frames = [
        frame
        for code, df in all_df_map.items()
        if (frame := _symbol_flags(code, df, name_map.get(code, ""))) is not None
    ]
    if not frames:
        return pd.DataFrame(columns=list(SENTIMENT_COLUMNS))
    flags = pd.concat(frames, ignore_index=True)
    grouped = flags.groupby("date", sort=True)
    out = pd.DataFrame(
        {
            "symbols": grouped.size(),
            "advancers": grouped["advance"].sum(),
            "decliners": grouped["decline"].sum(),
            "up_limit": grouped["up_limit"].sum(),
            "down_limit": grouped["down_limit"].sum(),
            "touched_up": grouped["touched_up"].sum(),
            "broke_up": grouped["broke_up"].sum(),
            "max_streak": grouped["streak"].max(),
        }
    )
    return out.reset_index().astype({col: int for col in SENTIMENT_COLUMNS if col != "date"})


def _symbol_flags(code: str, df: pd.DataFrame, name: str) -> pd.DataFrame | None:
    pct = limit_pct(code, name)
    if pct is None or df is None or len(df) < 2 or not {"date", "high", "close"}.issubset(df.columns):
        return None
    work = df.sort_values("date").reset_index(drop=True)
    close = pd.to_numeric(work["close"], errors="coerce")
    prev_close = close.shift(1)
    high = pd.to_numeric(work["high"], errors="coerce")
    dates = pd.to_datetime(work["date"], errors="coerce").dt.date
    threshold = _threshold_series(code, pct, dates)
    close_move = (close / prev_close - 1.0) * 100.0
    high_move = (high / prev_close - 1.0) * 100.0
    valid = prev_close.gt(0) & close_move.notna() & threshold.notna()
    up_limit = valid & (close_move >= threshold)
    touched_up = valid & (high_move >= threshold)
    out = pd.DataFrame(
        {
            "date": dates,
            "advance": valid & (close_move > 0),
            "decline": valid & (close_move < 0),
            "up_limit": up_limit,
            "down_limit": valid & (close_move <= -threshold),
            "touched_up": touched_up,
            "broke_up": touched_up & ~up_limit,
            "streak": _streak(up_limit.to_numpy()),
        }
    )
    return out[valid.to_numpy()]


def _threshold_series(code: str, pct: float, dates: pd.Series) -> pd.Series:
    base = pct * (1.0 - LIMIT_TOUCH_TOLERANCE)
    if cn_board(code) != "chinext":
        return pd.Series(base, index=dates.index)
    early = dates.map(lambda d: d is not None and d < _CHINEXT_20PCT_START)
    return pd.Series(np.where(early, 10.0 * (1.0 - LIMIT_TOUCH_TOLERANCE), base), index=dates.index)


def _streak(up_limit: np.ndarray) -> np.ndarray:
    out = np.zeros(len(up_limit), dtype=int)
    run = 0
    for idx, flag in enumerate(up_limit):
        run = run + 1 if flag else 0
        out[idx] = run
    return out
