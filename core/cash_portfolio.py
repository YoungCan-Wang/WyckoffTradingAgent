"""Cash-account portfolio simulation for backtest trades."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from core.candidate_policy import candidate_score_value

SUPPORTED_PORTFOLIO_STYLES = (
    "slot_equal_4",
    "probe_add",
    "confirmation_only",
    "trend_pyramid",
    "concentrated_swap",
)
STYLE_LABELS = {
    "slot_equal_4": "等额四仓",
    "probe_add": "观察仓补仓",
    "confirmation_only": "跨日确认买入",
    "trend_pyramid": "趋势金字塔",
    "concentrated_swap": "集中换股",
}
STYLE_PRESETS = {
    "default": ("slot_equal_4",),
    "all": SUPPORTED_PORTFOLIO_STYLES,
    "all_core": SUPPORTED_PORTFOLIO_STYLES,
    "style_lab": SUPPORTED_PORTFOLIO_STYLES,
}


@dataclass(frozen=True)
class CashPortfolioConfig:
    initial_cash: float = 100_000.0
    max_positions: int = 4
    commission_rate: float = 0.0002
    small_trade_threshold: float = 10_000.0
    small_trade_fee: float = 5.0
    lot_size: int = 100
    portfolio_style: str = "slot_equal_4"
    probe_weight: float = 0.125
    equal_weight: float = 0.25
    trend_initial_weight: float = 0.20
    trend_target_weight: float = 0.30
    concentrated_max_positions: int = 2
    swap_score_multiplier: float = 1.15
    buy_friction_pct: float = 0.0
    sell_friction_pct: float = 0.0


def expand_portfolio_styles(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    tokens = raw if isinstance(raw, list | tuple) else str(raw or "slot_equal_4").split(",")
    out: list[str] = []
    for token in tokens:
        key = str(token or "").strip().lower()
        if not key:
            continue
        values = STYLE_PRESETS.get(key, (key,))
        for value in values:
            if value not in SUPPORTED_PORTFOLIO_STYLES:
                raise ValueError(f"未知 portfolio_style: {value}")
            if value not in out:
                out.append(value)
    return out or ["slot_equal_4"]


def calc_commission(amount: float, config: CashPortfolioConfig) -> float:
    gross = max(float(amount), 0.0)
    if gross <= 0:
        return 0.0
    if gross < float(config.small_trade_threshold):
        return float(config.small_trade_fee)
    return gross * float(config.commission_rate)


def _style(config: CashPortfolioConfig) -> str:
    return expand_portfolio_styles(config.portfolio_style)[0]


def _style_label(config: CashPortfolioConfig) -> str:
    style = _style(config)
    return STYLE_LABELS.get(style, style)


def _position_limit(config: CashPortfolioConfig) -> int:
    if _style(config) == "concentrated_swap":
        return max(1, min(int(config.max_positions), int(config.concentrated_max_positions)))
    return max(1, int(config.max_positions))


def _portfolio_equity(
    cash: float,
    active: list[dict[str, Any]],
    day: date | None = None,
    mark_price_fn: Callable[[str, date], float | None] | None = None,
) -> float:
    value = float(cash)
    for pos in active:
        price = float(pos["entry_market_price"])
        if day is not None and mark_price_fn is not None:
            mark = mark_price_fn(str(pos["code"]), day)
            if mark is not None and mark > 0:
                price = float(mark)
        value += float(pos["shares"]) * price
    return value


def _code_exposure(
    active: list[dict[str, Any]],
    code: str,
    day: date,
    mark_price_fn: Callable[[str, date], float | None] | None,
) -> float:
    exposure = 0.0
    mark = mark_price_fn(code, day) if mark_price_fn is not None else None
    for pos in active:
        if pos["code"] != code:
            continue
        price = float(mark) if mark is not None and mark > 0 else float(pos["entry_market_price"])
        exposure += float(pos["shares"]) * price
    return exposure


def _active_codes(active: list[dict[str, Any]]) -> set[str]:
    return {str(pos.get("code", "")).strip() for pos in active if str(pos.get("code", "")).strip()}


def _row_score(row: pd.Series) -> float:
    return candidate_score_value(row.get("score"))


def _row_entry_weight_multiplier(row: pd.Series) -> float:
    try:
        value = float(row.get("entry_weight_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return min(max(value, 0.0), 1.0)


def _shares_for_budget(price: float, cash: float, budget: float, config: CashPortfolioConfig) -> int:
    lot_size = max(int(config.lot_size), 1)
    usable = max(min(float(cash), float(budget)), 0.0)
    shares = int(usable // (float(price) * lot_size)) * lot_size
    while shares > 0:
        gross = shares * float(price)
        if gross + calc_commission(gross, config) <= usable:
            return shares
        shares -= lot_size
    return 0


def _trade_calendar(df: pd.DataFrame) -> list[date]:
    """Union of entry/exit dates so a position's exit is never released late.

    Iterating only over signal (entry) days would delay releasing cash from positions whose
    exit_date falls in a gap between signals, understating available cash/slots for later entries.
    """
    return sorted(set(df["entry_date"].tolist()) | set(df["exit_date"].tolist()))


def _normalize_trade_dates(trades_df: pd.DataFrame) -> pd.DataFrame:
    required_cols = {"entry_date", "exit_date", "entry_close", "exit_close"}
    if trades_df is None or trades_df.empty or not required_cols.issubset(trades_df.columns):
        return pd.DataFrame()
    df = trades_df.copy()
    for col in ("signal_date", "entry_date", "exit_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["entry_close"] = pd.to_numeric(df.get("entry_close"), errors="coerce")
    df["exit_close"] = pd.to_numeric(df.get("exit_close"), errors="coerce")
    return df.dropna(subset=["entry_date", "exit_date", "entry_close", "exit_close"]).reset_index(drop=True)


def _numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def _close_position(
    pos: dict[str, Any],
    cash: float,
    price: float,
    closed_rows: list[dict[str, Any]],
    reason: str,
) -> float:
    config = pos["config"]
    execution_price = float(price) * (1.0 - float(config.sell_friction_pct) / 100.0)
    sell_gross = float(pos["shares"]) * execution_price
    sell_fee = calc_commission(sell_gross, config)
    sell_net = sell_gross - sell_fee
    pnl = sell_net - float(pos["cost_total"])
    cash += sell_net
    closed_rows.append(
        {
            **{k: v for k, v in pos.items() if k != "config"},
            "exit_market_price": float(price),
            "exit_price": execution_price,
            "sell_gross": sell_gross,
            "sell_friction_cost": float(pos["shares"]) * (float(price) - execution_price),
            "sell_fee": sell_fee,
            "pnl": pnl,
            "ret_pct": pnl / pos["cost_total"] * 100.0,
            "exit_reason": reason,
        }
    )
    return cash


def _close_due_positions(
    active: list[dict[str, Any]],
    cash: float,
    day: date,
    closed_rows: list[dict[str, Any]],
) -> float:
    keep: list[dict[str, Any]] = []
    for pos in active:
        if pos["exit_date"] > day:
            keep.append(pos)
            continue
        reason = str(pos.get("exit_reason") or "planned_exit")
        cash = _close_position(pos, cash, float(pos["exit_price"]), closed_rows, reason)
    active[:] = keep
    return cash


def _close_code_at_price(
    active: list[dict[str, Any]],
    cash: float,
    code: str,
    price: float,
    closed_rows: list[dict[str, Any]],
) -> float:
    keep: list[dict[str, Any]] = []
    for pos in active:
        if pos["code"] == code:
            cash = _close_position(pos, cash, price, closed_rows, "style_swap")
        else:
            keep.append(pos)
    active[:] = keep
    return cash


def _open_lot(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    *,
    weight: float,
    kind: str,
    target_weight: float | None = None,
    mark_price_fn: Callable[[str, date], float | None] | None = None,
) -> tuple[float, bool]:
    market_price = float(row["entry_close"])
    code = str(row.get("code", "")).strip()
    if market_price <= 0 or cash <= 0 or not code:
        return cash, False
    execution_price = market_price * (1.0 + float(config.buy_friction_pct) / 100.0)
    day = row["entry_date"]
    equity = _portfolio_equity(cash, active, day, mark_price_fn)
    multiplier = _row_entry_weight_multiplier(row)
    budget = equity * float(weight) * multiplier
    if target_weight is not None:
        target = equity * float(target_weight) * multiplier
        budget = max(target - _code_exposure(active, code, day, mark_price_fn), 0.0)
    shares = _shares_for_budget(execution_price, cash, budget, config)
    if shares <= 0:
        return cash, False
    buy_gross = shares * execution_price
    buy_fee = calc_commission(buy_gross, config)
    active.append(_new_position(row, config, kind, market_price, execution_price, shares, buy_gross, buy_fee))
    return cash - buy_gross - buy_fee, True


def _new_position(
    row: pd.Series,
    config: CashPortfolioConfig,
    kind: str,
    market_price: float,
    execution_price: float,
    shares: int,
    buy_gross: float,
    buy_fee: float,
) -> dict[str, Any]:
    return {
        "style": _style(config),
        "style_label": _style_label(config),
        "entry_kind": kind,
        "code": str(row.get("code", "")).strip(),
        "name": str(row.get("name", "") or row.get("code", "")).strip(),
        "signal_date": row.get("signal_date"),
        "entry_date": row["entry_date"],
        "exit_date": row["exit_date"],
        "entry_market_price": market_price,
        "entry_price": execution_price,
        "exit_price": float(row["exit_close"]),
        "shares": shares,
        "score": _row_score(row),
        "track": str(row.get("track", "") or ""),
        "trigger": str(row.get("trigger", "") or ""),
        "entry_weight_multiplier": _row_entry_weight_multiplier(row),
        "exit_reason": str(row.get("exit_reason", "") or ""),
        "buy_gross": buy_gross,
        "buy_friction_cost": shares * (execution_price - market_price),
        "buy_fee": buy_fee,
        "cost_total": buy_gross + buy_fee,
        "config": config,
    }


def _try_slot_equal(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
) -> float:
    code = str(row.get("code", "")).strip()
    if len(_active_codes(active)) >= _position_limit(config):
        skipped["full"] += 1
        return cash
    if code in _active_codes(active):
        skipped["duplicate"] += 1
        return cash
    cash, opened = _open_lot(
        row,
        cash,
        active,
        config,
        weight=1.0 / _position_limit(config),
        kind="initial",
        mark_price_fn=mark_price_fn,
    )
    skipped["cash"] += 0 if opened else 1
    return cash


def _try_probe_add(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
) -> float:
    code = str(row.get("code", "")).strip()
    if code in _active_codes(active):
        cash, opened = _open_lot(
            row,
            cash,
            active,
            config,
            weight=0.0,
            kind="add",
            target_weight=config.equal_weight,
            mark_price_fn=mark_price_fn,
        )
        skipped["weight_cap"] += 0 if opened else 1
        return cash
    if len(_active_codes(active)) >= _position_limit(config):
        skipped["full"] += 1
        return cash
    cash, opened = _open_lot(
        row, cash, active, config, weight=config.probe_weight, kind="probe", mark_price_fn=mark_price_fn
    )
    skipped["cash"] += 0 if opened else 1
    return cash


def _try_confirmation_only(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
) -> float:
    code = str(row.get("code", "")).strip()
    if not _row_signal_confirmed(row):
        skipped["unconfirmed"] += 1
        return cash
    if code in _active_codes(active):
        skipped["duplicate"] += 1
        return cash
    if len(_active_codes(active)) >= _position_limit(config):
        skipped["full"] += 1
        return cash
    cash, opened = _open_lot(
        row, cash, active, config, weight=config.equal_weight, kind="confirmed", mark_price_fn=mark_price_fn
    )
    skipped["cash"] += 0 if opened else 1
    return cash


def _row_signal_confirmed(row: pd.Series) -> bool:
    value = row.get("signal_confirmed", False)
    if pd.isna(value):
        return False
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "confirmed"}


def _try_trend_pyramid(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
) -> float:
    code = str(row.get("code", "")).strip()
    if code in _active_codes(active):
        if _is_trend_signal(row):
            cash, opened = _open_lot(
                row,
                cash,
                active,
                config,
                weight=0,
                kind="pyramid_add",
                target_weight=config.trend_target_weight,
                mark_price_fn=mark_price_fn,
            )
            skipped["weight_cap"] += 0 if opened else 1
        else:
            skipped["duplicate"] += 1
        return cash
    if len(_active_codes(active)) >= _position_limit(config):
        skipped["full"] += 1
        return cash
    cash, opened = _open_lot(
        row,
        cash,
        active,
        config,
        weight=config.trend_initial_weight,
        kind="trend_initial",
        mark_price_fn=mark_price_fn,
    )
    skipped["cash"] += 0 if opened else 1
    return cash


def _is_trend_signal(row: pd.Series) -> bool:
    trigger = str(row.get("trigger", "") or "").lower()
    return str(row.get("track", "") or "") == "Trend" or any(x in trigger for x in ("sos", "evr", "markup"))


def _weakest_active_code(active: list[dict[str, Any]]) -> tuple[str, float]:
    scores: dict[str, float] = {}
    for pos in active:
        scores[pos["code"]] = max(scores.get(pos["code"], float("-inf")), candidate_score_value(pos.get("score")))
    return min(scores.items(), key=lambda item: item[1]) if scores else ("", 0.0)


def _try_concentrated_swap(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
    closed: list[dict[str, Any]],
) -> float:
    code = str(row.get("code", "")).strip()
    if code in _active_codes(active):
        skipped["duplicate"] += 1
        return cash
    if len(_active_codes(active)) >= _position_limit(config):
        cash = _maybe_swap_weakest(row, cash, active, config, skipped, mark_price_fn, closed)
        if len(_active_codes(active)) >= _position_limit(config):
            return cash
    cash, opened = _open_lot(
        row,
        cash,
        active,
        config,
        weight=1.0 / _position_limit(config),
        kind="concentrated",
        mark_price_fn=mark_price_fn,
    )
    skipped["cash"] += 0 if opened else 1
    return cash


def _maybe_swap_weakest(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
    closed: list[dict[str, Any]],
) -> float:
    weak_code, weak_score = _weakest_active_code(active)
    if not weak_code or _row_score(row) < weak_score * float(config.swap_score_multiplier):
        skipped["not_stronger"] += 1
        return cash
    price = mark_price_fn(weak_code, row["entry_date"]) if mark_price_fn else None
    if price is None or price <= 0:
        skipped["full"] += 1
        return cash
    skipped["style_swaps"] += 1
    return _close_code_at_price(active, cash, weak_code, price, closed)


def _new_skipped() -> dict[str, int]:
    return {
        "full": 0,
        "cash": 0,
        "duplicate": 0,
        "unconfirmed": 0,
        "weight_cap": 0,
        "not_stronger": 0,
        "style_swaps": 0,
    }


def _portfolio_summary(
    closed_df: pd.DataFrame,
    nav_df: pd.DataFrame,
    cash: float,
    config: CashPortfolioConfig,
    skipped: dict[str, int],
) -> dict[str, Any]:
    ret = _numeric_column(closed_df, "ret_pct").dropna()
    wins = ret[ret > 0]
    losses = ret[ret < 0]
    entry_kind = (
        closed_df.get("entry_kind", pd.Series(dtype=str)).astype(str) if not closed_df.empty else pd.Series(dtype=str)
    )
    exit_reason = (
        closed_df.get("exit_reason", pd.Series(dtype=str)).astype(str) if not closed_df.empty else pd.Series(dtype=str)
    )
    return {
        "cash_portfolio_style": _style(config),
        "cash_portfolio_style_label": _style_label(config),
        "cash_portfolio_initial_cash": float(config.initial_cash),
        "cash_portfolio_final_cash": float(cash),
        "cash_portfolio_total_return_pct": (float(cash) / float(config.initial_cash) - 1.0) * 100.0,
        "cash_portfolio_max_drawdown_pct": _cash_nav_max_drawdown_pct(nav_df, float(config.initial_cash)),
        "cash_portfolio_trades": int(len(ret)),
        "cash_portfolio_win_rate_pct": float((ret > 0).mean() * 100.0) if len(ret) else None,
        "cash_portfolio_avg_profit_pct": float(wins.mean()) if len(wins) else None,
        "cash_portfolio_avg_loss_pct": float(losses.mean()) if len(losses) else None,
        "cash_portfolio_buy_friction_pct": float(config.buy_friction_pct),
        "cash_portfolio_sell_friction_pct": float(config.sell_friction_pct),
        "cash_portfolio_friction_total": float(_numeric_column(closed_df, "buy_friction_cost").sum())
        + float(_numeric_column(closed_df, "sell_friction_cost").sum()),
        "cash_portfolio_commission_total": float(_numeric_column(closed_df, "buy_fee").sum())
        + float(_numeric_column(closed_df, "sell_fee").sum()),
        "cash_portfolio_probe_entries": int(entry_kind.isin({"probe"}).sum()),
        "cash_portfolio_add_entries": int(entry_kind.isin({"add", "pyramid_add"}).sum()),
        "cash_portfolio_confirmed_entries": int(entry_kind.isin({"confirmed"}).sum()),
        "cash_portfolio_swap_exits": int((exit_reason == "style_swap").sum()),
        "cash_portfolio_unconfirmed": int(skipped.get("unconfirmed", 0)),
        "cash_portfolio_skipped_full": int(skipped.get("full", 0)),
        "cash_portfolio_skipped_cash": int(skipped.get("cash", 0)),
        "cash_portfolio_skipped_duplicate": int(skipped.get("duplicate", 0)),
        "cash_portfolio_skipped_weight_cap": int(skipped.get("weight_cap", 0)),
        "cash_portfolio_skipped_not_stronger": int(skipped.get("not_stronger", 0)),
        "cash_portfolio_style_swaps": int(skipped.get("style_swaps", 0)),
        "cash_portfolio_max_positions": int(_position_limit(config)),
    }


def _cash_nav_max_drawdown_pct(nav_df: pd.DataFrame, initial_cash: float) -> float | None:
    if initial_cash <= 0:
        return None
    if nav_df is None or nav_df.empty or "equity" not in nav_df.columns:
        return 0.0
    equity = pd.to_numeric(nav_df["equity"], errors="coerce").dropna()
    if equity.empty:
        return 0.0
    nav = pd.concat([pd.Series([initial_cash], dtype=float), equity.reset_index(drop=True)], ignore_index=True)
    peak = nav.cummax()
    drawdown = nav / peak - 1.0
    return float(drawdown.min() * 100.0)


def simulate_cash_portfolio(
    trades_df: pd.DataFrame,
    config: CashPortfolioConfig | None = None,
    mark_price_fn: Callable[[str, date], float | None] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    cfg = config or CashPortfolioConfig()
    df = _normalize_trade_dates(trades_df)
    if df.empty:
        empty = pd.DataFrame()
        return empty, empty, _portfolio_summary(empty, empty, cfg.initial_cash, cfg, _new_skipped())

    cash = float(cfg.initial_cash)
    active: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    skipped = _new_skipped()
    ordered = df.assign(_order=range(len(df))).sort_values(["entry_date", "_order"])
    signals_by_day = {day: rows for day, rows in ordered.groupby("entry_date")}

    for day in _trade_calendar(ordered):
        cash = _close_due_positions(active, cash, day, closed)
        for _, row in signals_by_day.get(day, pd.DataFrame()).iterrows():
            cash = _apply_style(row, cash, active, closed, cfg, skipped, mark_price_fn)
        equity_rows.append(
            {
                "date": day,
                "equity": _portfolio_equity(cash, active, day, mark_price_fn),
                "cash": cash,
                "positions": len(_active_codes(active)),
            }
        )

    closed_df = pd.DataFrame(closed)
    nav_df = pd.DataFrame(equity_rows)
    if not nav_df.empty:
        nav_df = nav_df.drop_duplicates(subset=["date"], keep="last")
    return closed_df, nav_df, _portfolio_summary(closed_df, nav_df, cash, cfg, skipped)


def _apply_style(
    row: pd.Series,
    cash: float,
    active: list[dict[str, Any]],
    closed: list[dict[str, Any]],
    config: CashPortfolioConfig,
    skipped: dict[str, int],
    mark_price_fn: Callable[[str, date], float | None] | None,
) -> float:
    style = _style(config)
    if style == "slot_equal_4":
        return _try_slot_equal(row, cash, active, config, skipped, mark_price_fn)
    if style == "probe_add":
        return _try_probe_add(row, cash, active, config, skipped, mark_price_fn)
    if style == "confirmation_only":
        return _try_confirmation_only(row, cash, active, config, skipped, mark_price_fn)
    if style == "trend_pyramid":
        return _try_trend_pyramid(row, cash, active, config, skipped, mark_price_fn)
    if style == "concentrated_swap":
        return _try_concentrated_swap(row, cash, active, config, skipped, mark_price_fn, closed)
    raise ValueError(f"未知 portfolio_style: {style}")
