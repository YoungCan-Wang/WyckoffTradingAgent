"""Backtest trade and portfolio metric helpers."""

from __future__ import annotations

import pandas as pd

from core._price_math import numeric_column as _numeric_series

DEFAULT_METRIC_HOLD_DAYS = 10


def fmt_metric(value: float | int | str | None, ndigits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{ndigits}f}"
    return str(value)


def calc_max_drawdown_pct(ret: pd.Series) -> float | None:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if s.empty:
        return None
    nav = 1.0 + (s / 100.0).cumsum()
    peak = nav.cummax()
    drawdown = nav / peak - 1.0
    return None if drawdown.empty else float(drawdown.min() * 100.0)


def calc_nav_max_drawdown_pct(nav: pd.Series) -> float | None:
    """净值口径回撤。`calc_max_drawdown_pct` 走的是逐笔收益算术累加，长周期净值不能用那个。"""
    drawdown = nav / nav.cummax() - 1.0
    return float(drawdown.min()) * 100.0 if not drawdown.empty else None


def calc_cvar95_pct(ret: pd.Series) -> tuple[float | None, float | None]:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if s.empty:
        return None, None
    var95 = float(s.quantile(0.05))
    tail = s[s <= var95]
    return (var95, None) if tail.empty else (var95, float(tail.mean()))


def calc_max_consecutive_losses(ret: pd.Series) -> int:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    max_streak = 0
    streak = 0
    for value in s.tolist():
        if float(value) < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return int(max_streak)


def calc_sharpe_ratio(
    ret: pd.Series,
    risk_free_annual: float = 2.0,
    periods_per_year: float | None = None,
    hold_days: int = DEFAULT_METRIC_HOLD_DAYS,
) -> float | None:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if len(s) < 3:
        return None
    mean_pct = float(s.mean())
    std_pct = float(s.std(ddof=1))
    if std_pct <= 0:
        return None
    periods = periods_per_year or 250.0 / max(hold_days, 1)
    ann_ret = mean_pct * periods / 100.0
    ann_std = std_pct * (periods**0.5) / 100.0
    return float((ann_ret - risk_free_annual / 100.0) / ann_std)


def calc_calmar_ratio(
    ret: pd.Series,
    periods_per_year: float | None = None,
    hold_days: int = DEFAULT_METRIC_HOLD_DAYS,
) -> float | None:
    s = pd.to_numeric(ret, errors="coerce").dropna()
    if len(s) < 3:
        return None
    mdd = calc_max_drawdown_pct(s)
    if mdd is None or mdd >= 0:
        return None
    periods = periods_per_year or 250.0 / max(hold_days, 1)
    return float(float(s.mean()) * periods / abs(mdd))


def calc_information_ratio(
    ret: pd.Series, bench_ret: pd.Series | None, periods_per_year: float = 250.0
) -> float | None:
    if bench_ret is None:
        return None
    s = pd.to_numeric(ret, errors="coerce").dropna()
    b = pd.to_numeric(bench_ret, errors="coerce").dropna()
    n = min(len(s), len(b))
    if n < 3:
        return None
    excess = s.iloc[:n].values - b.iloc[:n].values
    excess_std = float(excess.std(ddof=1))
    if excess_std <= 0:
        return None
    ann_excess = float(excess.mean()) * periods_per_year / 100.0
    ann_te = excess_std * (periods_per_year**0.5) / 100.0
    return float(ann_excess / ann_te)


def stats_for_trade_slice(df_slice: pd.DataFrame, hold_days: int = DEFAULT_METRIC_HOLD_DAYS) -> dict:
    ret = pd.to_numeric(df_slice.get("ret_pct"), errors="coerce").dropna()
    if len(ret) == 0:
        return {"trades": 0}
    var95, cvar95 = calc_cvar95_pct(ret)
    exit_reason = df_slice.get("exit_reason", pd.Series(dtype=str)).astype(str)
    mfe = _numeric_series(df_slice, "mfe_pct")
    mae = _numeric_series(df_slice, "mae_pct")
    return {
        "trades": len(ret),
        "win_rate_pct": float((ret > 0).mean() * 100.0),
        "avg_ret_pct": float(ret.mean()),
        "median_ret_pct": float(ret.median()),
        "max_drawdown_pct": calc_max_drawdown_pct(ret),
        "sharpe_ratio": calc_sharpe_ratio(ret, hold_days=hold_days),
        "calmar_ratio": calc_calmar_ratio(ret, hold_days=hold_days),
        "var95_ret_pct": var95,
        "cvar95_ret_pct": cvar95,
        "max_consecutive_losses": calc_max_consecutive_losses(ret),
        "stop_exit_rate_pct": float(exit_reason.isin({"stop_loss", "atr_stop"}).mean() * 100.0)
        if len(exit_reason)
        else None,
        "avg_mfe_pct": float(mfe.mean()) if len(mfe) else None,
        "avg_mae_pct": float(mae.mean()) if len(mae) else None,
    }


def group_trade_stats(trades_df: pd.DataFrame, column: str, hold_days: int) -> dict[str, dict]:
    if trades_df.empty or column not in trades_df.columns:
        return {}
    return {
        str(value).strip() or "-": stats_for_trade_slice(trades_df[trades_df[column] == value], hold_days)
        for value in sorted(trades_df[column].dropna().unique(), key=str)
    }


def calc_stratified_stats(trades_df: pd.DataFrame, hold_days: int = DEFAULT_METRIC_HOLD_DAYS) -> dict[str, dict]:
    result = {key: {} for key in ("by_track", "by_regime", "by_trigger", "by_exit_reason", "by_entry_price_source")}
    if trades_df.empty:
        return result
    for track in ("Trend", "Accum"):
        mask = trades_df["track"] == track
        if mask.any():
            result["by_track"][track] = stats_for_trade_slice(trades_df[mask], hold_days)
    result["by_regime"] = group_trade_stats(trades_df, "regime", hold_days)
    result["by_trigger"] = group_trade_stats(trades_df, "trigger", hold_days)
    result["by_exit_reason"] = group_trade_stats(trades_df, "exit_reason", hold_days)
    result["by_entry_price_source"] = group_trade_stats(trades_df, "entry_price_source", hold_days)
    cross = _track_regime_stats(trades_df, hold_days)
    if cross:
        result["by_track_regime"] = cross
    return result


def _track_regime_stats(trades_df: pd.DataFrame, hold_days: int) -> dict[str, dict]:
    if "regime" not in trades_df.columns:
        return {}
    out: dict[str, dict] = {}
    for track in ("Trend", "Accum"):
        for regime in trades_df["regime"].dropna().unique():
            regime_str = str(regime).strip()
            mask = (trades_df["track"] == track) & (trades_df["regime"] == regime_str)
            if mask.any():
                out[f"{track}_{regime_str}"] = stats_for_trade_slice(trades_df[mask], hold_days)
    return out
