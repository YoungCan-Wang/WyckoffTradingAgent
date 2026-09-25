"""Agent-facing corporate news observations, with no discovery-time I/O."""

from __future__ import annotations

import logging
from typing import Any

from agents.tool_context import ToolContext
from core.corporate_event_scan import render_corporate_event_report

logger = logging.getLogger(__name__)


def scan_corporate_events(limit: int = 20, tool_context: ToolContext | None = None) -> dict[str, Any]:
    from workflows.corporate_event_scan_runtime import run_corporate_event_scan

    del tool_context
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return {"error": "limit 必须是 1–50 的整数", "hits": []}
    try:
        result = run_corporate_event_scan(persist=False)
    except Exception as exc:
        logger.warning("scan_corporate_events failed (%s)", type(exc).__name__)
        return {
            "error": "消息源暂时不可用，不要据此断定没有停牌或重组。",
            "source_ok": False,
            "source_status": "unavailable",
            "hits": [],
        }
    payload = result.payload()
    payload.update(
        hits=payload["hits"][:limit], undated_hits=payload["undated_hits"][:limit], total_hits=len(result.hits)
    )
    payload["report"] = render_corporate_event_report(
        result.hits[:limit],
        as_of=result.as_of,
        source_status=result.source_status,
        failed_sources=tuple(source.source for source in result.sources if not source.ok),
        undated_count=len(result.undated_hits),
        source_details=[source.as_dict() for source in result.sources],
    )
    if result.source_status == "unavailable":
        payload["error"] = "全部消息源不可用；无法判断是否有事件。"
    return payload
