from __future__ import annotations

import integrations.browser_cdp as browser_cdp


def test_resolve_cdp_url_rejects_non_local_hosts():
    assert "error" in browser_cdp.resolve_cdp_url("http://example.com:9222")
    assert "error" in browser_cdp.resolve_cdp_url("http://192.168.1.2:9222")
    assert browser_cdp.resolve_cdp_url("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    assert browser_cdp.resolve_cdp_url("http://localhost:9222") == "http://localhost:9222"


def test_parse_duckduckgo_results_unwraps_redirect_and_validates_urls():
    html = """
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fipo">宇树 IPO</a>
    <a class="result__a" href="http://127.0.0.1/secret">内网</a>
    <a class="result__a" href="https://example.org/a">第二条</a>
    """
    results = browser_cdp.parse_search_results(html, max_results=5, engine="duckduckgo")
    assert [item["url"] for item in results] == ["https://example.com/ipo", "https://example.org/a"]
    assert results[0]["title"] == "宇树 IPO"


def test_extract_readable_text_strips_markup_and_truncates():
    html = "<html><script>evil()</script><style>b{}</style><body><h1>标题</h1><p>正文内容</p></body></html>"
    text = browser_cdp.extract_readable_text(html, limit=8)
    assert "evil" not in text
    assert "标题" in text or text.startswith("标题") or "正" in text
    assert len(text) <= 8


def test_browser_cdp_status_reports_missing_playwright(monkeypatch):
    monkeypatch.setattr(browser_cdp, "resolve_cdp_url", lambda _raw=None: "http://127.0.0.1:9222")

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    status = browser_cdp.browser_cdp_status("http://127.0.0.1:9222")
    assert status["ok"] is False
    assert "playwright" in status["error"].lower()


def test_research_via_cdp_requires_query():
    assert browser_cdp.research_via_cdp("  ")["error"] == "query 不能为空"


def test_research_via_cdp_returns_hint_when_connect_fails(monkeypatch):
    class _Playwright:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def chromium(self):
            return self

        def connect_over_cdp(self, *_args, **_kwargs):
            raise RuntimeError("ECONNREFUSED")

    monkeypatch.setattr(browser_cdp, "resolve_cdp_url", lambda _raw=None: "http://127.0.0.1:9222")
    monkeypatch.setattr(
        browser_cdp,
        "search_engine_url",
        lambda query, engine=None: f"https://html.duckduckgo.com/html/?q={query}",
    )

    import sys
    from types import ModuleType

    fake_sync = ModuleType("playwright.sync_api")
    fake_sync.sync_playwright = lambda: _Playwright()
    fake_root = ModuleType("playwright")
    fake_root.sync_api = fake_sync
    monkeypatch.setitem(sys.modules, "playwright", fake_root)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)

    result = browser_cdp.research_via_cdp("宇树科技 IPO")
    assert "无法连接 CDP" in result["error"]
    assert "remote-debugging-port" in result["hint"]
