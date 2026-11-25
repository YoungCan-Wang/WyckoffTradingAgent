"""Agent-facing tool for the standalone medium-term Institutional Slow-Bull scanner."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from agents.tool_context import ToolContext, ensure_tushare_token
from core.candidate_metadata import code6

logger = logging.getLogger(__name__)

_VALID_BOARDS = {"all", "main", "chinext", "star", "bse", "main_chinext_star"}
_BOARD_ALIASES = {
    "主板": "main",
    "创业板": "chinext",
    "科创板": "star",
    "北交所": "bse",
    "沪深a股": "main_chinext_star",
    "全部": "all",
}


def _normalize_codes(raw: list[str] | str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        for part in str(item or "").replace("，", ",").split(","):
            code = code6(part.strip())
            if code and code not in seen:
                seen.add(code)
                out.append(code)
    return tuple(out)


def _normalize_board(raw: str | None) -> str:
    text = str(raw or "main_chinext_star").strip().lower()
    mapped = _BOARD_ALIASES.get(text, text)
    if mapped not in _VALID_BOARDS:
        raise ValueError(f"不支持的板块 '{raw}'，可选: {', '.join(sorted(_VALID_BOARDS))}")
    return mapped


def _parse_as_of_date(raw: str | None):
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError("as_of_date 必须是 YYYY-MM-DD 或 YYYYMMDD")


def remember_slow_bull_scan(tool_context: ToolContext | None, result: dict[str, Any]) -> None:
    """Persist compact slow-bull scan summary onto tool_context.state."""
    if tool_context is None or result.get("error"):
        return
    compact = {
        "trade_date": result.get("trade_date"),
        "mode": result.get("mode"),
        "counts": result.get("counts"),
        "climate": result.get("climate"),
        "result_summary": result.get("result_summary"),
        "emerge_codes": [item.get("code") for item in (result.get("emerge_candidates") or [])[:10]],
        "pullback_codes": [item.get("code") for item in (result.get("pullback_candidates") or [])[:10]],
        "watch_codes": [item.get("code") for item in (result.get("watch_candidates") or [])[:10]],
    }
    with tool_context.state_lock:
        tool_context.state["last_slow_bull_scan"] = compact


def scan_slow_bull(
    codes: list[str] | str | None = None,
    board: str = "main_chinext_star",
    limit: int = 0,
    as_of_date: str = "",
    include_financial_metrics: bool = True,
    allow_cycle_cashflow_bypass: bool = False,
    top_n: int = 15,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Run post-market FIP Slow-Bull scan or targeted slow-bull diagnosis."""
    try:
        ensure_tushare_token(tool_context)
        from workflows.slow_bull_scan import SlowBullScanRequest, run_slow_bull_scan

        request = SlowBullScanRequest(
            codes=_normalize_codes(codes),
            board=_normalize_board(board),
            limit=min(max(int(limit), 0), 3000),
            as_of_date=_parse_as_of_date(as_of_date),
            include_financial_metrics=bool(include_financial_metrics),
            allow_cycle_cashflow_bypass=bool(allow_cycle_cashflow_bypass),
            top_n=min(max(int(top_n), 1), 50),
        )
        result = run_slow_bull_scan(request)
        remember_slow_bull_scan(tool_context, result)
        return result
    except Exception as exc:
        logger.exception("scan_slow_bull error")
        return {
            "error": str(exc),
            "hint": "检查行情/Tushare/TickFlow 凭据及参数格式（as_of_date=YYYY-MM-DD, board=main_chinext_star）。",
        }
