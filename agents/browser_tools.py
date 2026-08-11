"""CLI agent tools that drive a local Chrome CDP session."""

from __future__ import annotations

from typing import Any

from integrations.browser_cdp import ensure_cdp_session, research_via_cdp


def browser_research(
    query: str,
    max_results: int = 5,
    max_pages: int = 3,
    tool_context: Any = None,
) -> dict[str, Any]:
    """Search the public web via the user's local Chrome CDP and return citations."""
    consent_cb = None
    if tool_context is not None:
        consent_cb = getattr(tool_context, "state", {}).get("ensure_browser_cdp")
        if not callable(consent_cb):
            consent_cb = None
    ensured = ensure_cdp_session(consent_cb)
    if not ensured.get("ok"):
        return {
            "error": ensured.get("error") or "本机 Chrome CDP 未就绪",
            "hint": ensured.get("hint"),
            "cdp_url": ensured.get("cdp_url"),
        }
    result = research_via_cdp(query, max_results=max_results, max_pages=max_pages)
    if ensured.get("launched"):
        result = dict(result)
        result["cdp_launched"] = True
        if ensured.get("pid") is not None:
            result["cdp_pid"] = ensured.get("pid")
    return result
