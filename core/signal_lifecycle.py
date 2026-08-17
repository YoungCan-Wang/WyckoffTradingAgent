"""Signal lifecycle evaluation for Wyckoff events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

from core._price_math import to_numeric as _to_numeric
from core.trade_friction import net_return_pct


@dataclass(frozen=True)
class HorizonOutcome:
    horizon: int
    status: str
    return_pct: float | None
    max_drawdown_pct: float | None
    # 扣除双边交易成本后的收益。return_pct 保持毛收益不动，便于与历史数据对比；
    # 所有「这个信号值不值得做」的判断都应该看 net_return_pct。
    net_return_pct: float | None = None


@dataclass(frozen=True)
class SignalLifecycle:
    code: str
    signal_date: str
    entry_price: float | None
    outcomes: tuple[HorizonOutcome, ...]


def _empty_lifecycle(code: str, signal_date: str | None) -> SignalLifecycle:
    return SignalLifecycle(code=str(code or ""), signal_date=str(signal_date or ""), entry_price=None, outcomes=())


def _prepared_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    if "date" in work.columns:
        work["_dt"] = pd.to_datetime(work["date"], errors="coerce")
        return work.sort_values("_dt").reset_index(drop=True)
    return work.reset_index(drop=False).rename(columns={"index": "_dt"})


def _signal_position(work: pd.DataFrame, signal_date: str | None) -> int:
    if signal_date and "date" in work.columns:
        target_dt = pd.to_datetime(signal_date, errors="coerce")
        idx_matches = work.index[pd.to_datetime(work["date"], errors="coerce") == target_dt].tolist()
        signal_pos = int(idx_matches[-1]) if idx_matches else int(len(work) - 1)
    else:
        signal_pos = int(len(work) - 1)
    return min(max(signal_pos, 0), len(work) - 1)


def _base_price(close: pd.Series, signal_pos: int, entry_price: float | None) -> float | None:
    signal_close = close.iloc[signal_pos]
    base_price = float(entry_price) if entry_price is not None else float(signal_close)
    if base_price <= 0 or pd.isna(base_price):
        return None
    return base_price


def _signal_date_string(work: pd.DataFrame, signal_pos: int) -> str:
    if "date" not in work.columns:
        return str(signal_pos)
    dt_value = work["date"].iloc[signal_pos]
    return str(pd.to_datetime(dt_value, errors="coerce").date())


def _horizon_outcome(
    horizon_raw: int,
    *,
    signal_pos: int,
    base_price: float | None,
    close: pd.Series,
    low: pd.Series,
) -> HorizonOutcome:
    horizon = max(int(horizon_raw), 1)
    end_pos = signal_pos + horizon
    if base_price is None or end_pos >= len(close):
        return HorizonOutcome(horizon=horizon, status="pending", return_pct=None, max_drawdown_pct=None)
    future_close = close.iloc[end_pos]
    if pd.isna(future_close):
        return HorizonOutcome(horizon=horizon, status="invalid", return_pct=None, max_drawdown_pct=None)
    path = low.iloc[signal_pos + 1 : end_pos + 1].dropna()
    min_path_price = min(base_price, float(path.min()) if not path.empty else float(future_close))
    ret = (float(future_close) - base_price) / base_price * 100.0
    mdd = (min_path_price - base_price) / base_price * 100.0
    return HorizonOutcome(
        horizon=horizon,
        status="done",
        return_pct=float(ret),
        max_drawdown_pct=float(mdd),
        net_return_pct=net_return_pct(float(ret)),
    )


def _horizon_outcomes(
    horizons: Iterable[int],
    *,
    signal_pos: int,
    base_price: float | None,
    close: pd.Series,
    low: pd.Series,
) -> tuple[HorizonOutcome, ...]:
    return tuple(
        _horizon_outcome(h, signal_pos=signal_pos, base_price=base_price, close=close, low=low) for h in horizons
    )


def evaluate_signal_lifecycle(
    df: pd.DataFrame,
    *,
    code: str = "",
    signal_date: str | None = None,
    entry_price: float | None = None,
    horizons: Iterable[int] = (1, 3, 5, 10),
) -> SignalLifecycle:
    """Evaluate forward returns after a signal date.

    If future bars are not available yet, the corresponding horizon is marked
    as ``pending``.  This makes the function usable both in historical replay
    and in today's live run.
    """

    if df is None or df.empty or "close" not in df.columns:
        return _empty_lifecycle(code, signal_date)

    work = _prepared_frame(df)
    close = _to_numeric(work["close"]).reset_index(drop=True)
    if close.dropna().empty:
        return _empty_lifecycle(code, signal_date)
    low = _to_numeric(work["low"]).reset_index(drop=True) if "low" in work.columns else close

    signal_pos = _signal_position(work, signal_date)
    base_price = _base_price(close, signal_pos, entry_price)
    signal_date_s = _signal_date_string(work, signal_pos)
    outcomes = _horizon_outcomes(horizons, signal_pos=signal_pos, base_price=base_price, close=close, low=low)

    return SignalLifecycle(
        code=str(code or ""),
        signal_date=signal_date_s,
        entry_price=base_price,
        outcomes=outcomes,
    )


__all__ = ["HorizonOutcome", "SignalLifecycle", "evaluate_signal_lifecycle"]
