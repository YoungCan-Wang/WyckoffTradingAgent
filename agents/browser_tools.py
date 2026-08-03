"""CLI agent tools that drive a local Chrome CDP session."""

from __future__ import annotations

from typing import Any

from integrations.browser_cdp import research_via_cdp


def browser_research(
    query: str,
    max_results: int = 5,
    max_pages: int = 3,
    tool_context: Any = None,
) -> dict[str, Any]:
    """Search the public web via the user's local Chrome CDP and return citations."""
    _ = tool_context
    return research_via_cdp(query, max_results=max_results, max_pages=max_pages)
