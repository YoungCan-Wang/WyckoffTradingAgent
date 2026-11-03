from __future__ import annotations

import logging
from datetime import date, timedelta

from agents.tool_context import ToolContext, ensure_tushare_token
from utils.trading_clock import cn_today

logger = logging.getLogger(__name__)


def run_backtest(
    start: str = "",
    end: str = "",
    hold_days: int = 10,
    top_n: int = 4,
    board: str = "all",
    stop_loss_pct: float = -8.0,
    take_profit_pct: float = 0.0,
    entry_price_mode: str = "open",
    tool_context: ToolContext | None = None,
) -> dict:
    try:
        ensure_tushare_token(tool_context)
        params = _resolve_backtest_params(
            start, end, hold_days, top_n, board, stop_loss_pct, take_profit_pct, entry_price_mode
        )
        from workflows.backtest import BacktestWorkflowRequest, run_backtest_request

        _trades_df, summary = run_backtest_request(BacktestWorkflowRequest(**params, **_portfolio_defaults()))
        return _backtest_summary(params, summary)
    except Exception as e:
        logger.exception("run_backtest error")
        return {"error": str(e)}


def _resolve_backtest_params(
    start: str,
    end: str,
    hold_days: int,
    top_n: int,
    board: str,
    stop_loss_pct: float,
    take_profit_pct: float,
    entry_price_mode: str = "open",
) -> dict:
    normalized_entry_mode = str(entry_price_mode or "open").strip().lower()
    if normalized_entry_mode not in {"open", "close", "tail_1455"}:
        normalized_entry_mode = "open"
    return {
        "start_dt": _parse_start_date(start),
        "end_dt": _parse_end_date(end),
        "hold_days": max(1, min(int(hold_days), 60)),
        "top_n": max(0, min(int(top_n), 20)),
        "board": str(board or "all").strip(),
        "sample_size": 0,
        "trading_days": 320,
        "max_workers": 8,
        "exit_mode": "sltp",
        "stop_loss_pct": min(0.0, float(stop_loss_pct)),
        "take_profit_pct": max(0.0, float(take_profit_pct)),
        "entry_price_mode": normalized_entry_mode,
    }


def _parse_start_date(raw: str) -> date:
    return date.fromisoformat(str(raw).strip()[:10]) if raw else cn_today() - timedelta(days=180)


def _parse_end_date(raw: str) -> date:
    return date.fromisoformat(str(raw).strip()[:10]) if raw else cn_today() - timedelta(days=1)


def _portfolio_defaults() -> dict:
    return {
        "cash_portfolio": True,
        "initial_cash": 100_000.0,
        "max_positions": 4,
        "portfolio_styles": "confirmation_only",
    }


def _backtest_summary(params: dict, summary: dict) -> dict:
    return {
        "period": f"{params['start_dt']} ~ {params['end_dt']}",
        "hold_days": params["hold_days"],
        "top_n": params["top_n"],
        "board": params["board"],
        "stop_loss_pct": params["stop_loss_pct"],
        "take_profit_pct": params["take_profit_pct"],
        "entry_price_mode": params["entry_price_mode"],
        "trades": summary.get("trades", 0),
        "win_rate_pct": summary.get("win_rate_pct"),
        "avg_ret_pct": summary.get("avg_ret_pct"),
        "median_ret_pct": summary.get("median_ret_pct"),
        "sharpe_ratio": summary.get("sharpe_ratio"),
        "max_drawdown_pct": summary.get("max_drawdown_pct"),
        "portfolio_total_ret_pct": summary.get("portfolio_total_ret_pct"),
        "portfolio_ann_ret_pct": summary.get("portfolio_ann_ret_pct"),
        "max_consecutive_losses": summary.get("max_consecutive_losses"),
        "cash_final": summary.get("cash_portfolio_final_cash"),
        "cash_return_pct": summary.get("cash_portfolio_total_return_pct"),
        "cash_max_drawdown_pct": summary.get("cash_portfolio_max_drawdown_pct"),
        "cash_trades": summary.get("cash_portfolio_trades"),
        "cash_style": summary.get("cash_portfolio_style"),
    }
