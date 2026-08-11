from __future__ import annotations

from types import SimpleNamespace

from agents import browser_tools


def test_browser_research_without_consent_callback_does_not_search(monkeypatch):
    monkeypatch.setattr(
        browser_tools,
        "ensure_cdp_session",
        lambda _cb=None: {"ok": False, "error": "need consent", "hint": "/browser start"},
    )
    called = {"n": 0}
    monkeypatch.setattr(
        browser_tools,
        "research_via_cdp",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"query": "x"},
    )
    out = browser_tools.browser_research("宇树 IPO")
    assert out["error"] == "need consent"
    assert called["n"] == 0


def test_browser_research_uses_tool_context_consent_and_retries(monkeypatch):
    monkeypatch.setattr(
        browser_tools,
        "ensure_cdp_session",
        lambda cb=None: (
            {"ok": True, "launched": True, "pid": 99, "browser": "Chrome"}
            if callable(cb) and cb() == "allow"
            else {"ok": False, "error": "denied"}
        ),
    )
    monkeypatch.setattr(
        browser_tools,
        "research_via_cdp",
        lambda query, max_results=5, max_pages=3: {"query": query, "results": []},
    )
    ctx = SimpleNamespace(state={"ensure_browser_cdp": lambda: "allow"})
    out = browser_tools.browser_research("宇树 IPO", tool_context=ctx)
    assert out["query"] == "宇树 IPO"
    assert out["cdp_launched"] is True
    assert out["cdp_pid"] == 99
