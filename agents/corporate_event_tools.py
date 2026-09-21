"""Agent-facing corporate event / halt scan. Observation only."""

from __future__ import annotations

import logging
from typing import Any

from agents.tool_context import ToolContext
from core.corporate_event_scan import render_corporate_event_report
from workflows.corporate_event_scan_runtime import run_corporate_event_scan, shanghai_now

logger = logging.getLogger(__name__)


def scan_corporate_events(limit: int = 20, tool_context: ToolContext | None = None) -> dict[str, Any]:
    """扫描已公告与媒体电报中的重大资产重组 / 停牌。不是实盘，也不改漏斗。"""
    del tool_context
    try:
        result = run_corporate_event_scan(persist=False)
    except Exception as exc:
        logger.exception("scan_corporate_events failed")
        return {"error": str(exc), "hits": [], "note": "消息源暂时不可用，不要据此断定没有停牌或重组。"}
    cap = max(min(int(limit or 20), 50), 1)
    hits = [hit.as_dict() for hit in result.hits[:cap]]
    as_of = result.as_of or shanghai_now().strftime("%Y-%m-%d %H:%M")
    return {
        "as_of": as_of,
        "note": "已公告与媒体电报观察，不是实盘，也不是漏斗买许可。",
        "source_ok": result.source_ok,
        "hits": hits,
        "report": render_corporate_event_report(result.hits[:cap], as_of=as_of),
    }
