"""Portfolio-level backtest calculations."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from core.backtest_metrics import (
    calc_cvar95_pct,
    calc_information_ratio,
    calc_nav_max_drawdown_pct,
    calc_sharpe_ratio,
)
from core.candidate_policy import candidate_score_value

PORTFOLIO_NAV_COLUMNS = ["date", "nav", "daily_ret_pct", "cash", "positions_count"]
_SCORE_VALUE_COL = "_score_value"


def build_portfolio_nav(
    trades_df: pd.DataFrame,
    *,
    initial_capital: float = 1_000_000.0,
    max_concurrent: int = 5,
    weight_mode: str = "equal",
) -> pd.DataFrame:
    """Build a portfolio NAV curve from trade-level backtest rows."""
    if trades_df.empty:
        return pd.DataFrame(columns=PORTFOLIO_NAV_COLUMNS)

    df = _normalize_trade_dates(trades_df)
    all_dates = _trade_calendar(df)
    if not all_dates:
        return pd.DataFrame(columns=PORTFOLIO_NAV_COLUMNS)

    nav_records: list[dict[str, Any]] = []
    cash = initial_capital
    active_positions: list[dict[str, Any]] = []
    for day in all_dates:
        cash, active_positions = _close_due_positions(cash, active_positions, day)
        cash, active_positions = _open_signals_for_day(
            cash=cash,
            active_positions=active_positions,
            new_signals=df[df["signal_date"] == day].copy(),
            max_concurrent=max_concurrent,
            weight_mode=weight_mode,
        )
        nav_records.append(_nav_record(day, cash, active_positions, nav_records, initial_capital))

    return pd.DataFrame(nav_records)


def calc_portfolio_metrics(
    nav_df: pd.DataFrame,
    bench_daily_ret: pd.Series | None = None,
    initial_capital: float = 1_000_000.0,
) -> dict[str, float | int | None]:
    """Calculate risk-adjusted portfolio metrics."""
    if nav_df.empty:
        return {}

    nav = pd.to_numeric(nav_df["nav"], errors="coerce").dropna()
    daily_ret = pd.to_numeric(nav_df["daily_ret_pct"], errors="coerce").dropna()
    total_ret_pct = (float(nav.iloc[-1]) / initial_capital - 1.0) * 100.0
    n_days = len(nav)
    ann_ret_pct = total_ret_pct * (250.0 / max(n_days, 1))
    max_dd_pct = calc_nav_max_drawdown_pct(nav)
    calmar = ann_ret_pct / abs(max_dd_pct) if max_dd_pct is not None and max_dd_pct < 0 else None
    var95, cvar95 = calc_cvar95_pct(daily_ret)
    pos_counts = pd.to_numeric(nav_df.get("positions_count"), errors="coerce").dropna()
    return {
        "total_return_pct": total_ret_pct,
        "annualized_return_pct": ann_ret_pct,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_ratio": calc_sharpe_ratio(daily_ret, periods_per_year=250.0),
        "calmar_ratio": calmar,
        "information_ratio": calc_information_ratio(daily_ret, bench_daily_ret, periods_per_year=250.0),
        "var95_daily_pct": var95,
        "cvar95_daily_pct": cvar95,
        "trading_days": n_days,
        "avg_positions": float(pos_counts.mean()) if not pos_counts.empty else 0,
        "max_positions": int(pos_counts.max()) if not pos_counts.empty else 0,
        "final_nav": float(nav.iloc[-1]),
    }


def _normalize_trade_dates(trades_df: pd.DataFrame) -> pd.DataFrame:
    df = trades_df.copy()
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date
    df["exit_date"] = pd.to_datetime(df["exit_date"]).dt.date
    return df


def _trade_calendar(df: pd.DataFrame) -> list[date]:
    return sorted(set(df["signal_date"].tolist() + df["exit_date"].tolist()))


def _close_due_positions(
    cash: float,
    active_positions: list[dict[str, Any]],
    day: date,
) -> tuple[float, list[dict[str, Any]]]:
    for pos in active_positions:
        if pos["exit_date"] <= day:
            cash += pos["entry_capital"] * (1.0 + pos["ret_pct"] / 100.0)
    remaining = [p for p in active_positions if p["exit_date"] > day]
    return cash, remaining


def _open_signals_for_day(
    *,
    cash: float,
    active_positions: list[dict[str, Any]],
    new_signals: pd.DataFrame,
    max_concurrent: int,
    weight_mode: str,
) -> tuple[float, list[dict[str, Any]]]:
    slots_available = max(max_concurrent - len(active_positions), 0)
    if slots_available <= 0 or new_signals.empty:
        return cash, active_positions
    selected = _rank_signals(new_signals).head(slots_available)
    allocable = cash * 0.95
    for (_, row), weight in zip(selected.iterrows(), _signal_weights(selected, weight_mode), strict=False):
        entry_capital = allocable * float(weight)
        if entry_capital <= 0:
            continue
        cash -= entry_capital
        active_positions.append(_position_from_signal(row, entry_capital))
    return cash, active_positions


def _signal_weights(new_signals: pd.DataFrame, weight_mode: str) -> pd.Series:
    count = len(new_signals)
    if weight_mode == "score" and "score" in new_signals.columns:
        scores = _signal_score_series(new_signals).clip(lower=0.0)
        total_score = scores.sum()
        if total_score > 0:
            return scores / total_score
    return pd.Series([1.0 / count] * count, index=new_signals.index)


def _rank_signals(new_signals: pd.DataFrame) -> pd.DataFrame:
    ranked = new_signals.assign(**{_SCORE_VALUE_COL: _signal_score_series(new_signals)})
    sort_cols = [_SCORE_VALUE_COL]
    ascending = [False]
    if "code" in ranked.columns:
        sort_cols.append("code")
        ascending.append(True)
    return ranked.sort_values(sort_cols, ascending=ascending).drop(columns=[_SCORE_VALUE_COL])


def _signal_score_series(new_signals: pd.DataFrame) -> pd.Series:
    if "score" not in new_signals.columns:
        return pd.Series([0.0] * len(new_signals), index=new_signals.index)
    return new_signals["score"].map(candidate_score_value)


def _position_from_signal(row: pd.Series, entry_capital: float) -> dict[str, Any]:
    return {
        "code": row.get("code", ""),
        "track": row.get("track", ""),
        "entry_capital": entry_capital,
        "ret_pct": float(row.get("ret_pct", 0.0)),
        "exit_date": row["exit_date"],
    }


def _nav_record(
    day: date,
    cash: float,
    active_positions: list[dict[str, Any]],
    nav_records: list[dict[str, Any]],
    initial_capital: float,
) -> dict[str, Any]:
    nav = cash + sum(p["entry_capital"] for p in active_positions)
    prev_nav = nav_records[-1]["nav"] if nav_records else initial_capital
    daily_ret = (nav / prev_nav - 1.0) * 100.0 if prev_nav > 0 else 0.0
    return {
        "date": day,
        "nav": nav,
        "daily_ret_pct": daily_ret,
        "cash": cash,
        "positions_count": len(active_positions),
    }
