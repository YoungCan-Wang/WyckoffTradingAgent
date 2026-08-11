"""Attach to a local Chrome DevTools endpoint for CLI browser research."""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from utils.http_security import redact_sensitive_text, validate_public_http_url

logger = logging.getLogger(__name__)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
MAX_PAGE_CHARS = 8000
CDP_WAIT_SECONDS = 20.0
CDP_POLL_INTERVAL = 0.4
SEARCH_ENGINES = {
    "bing": "https://www.bing.com/search?q={query}",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={query}",
}
# Empirically more reliable for automated SERP parsing than Bing in this environment.
DEFAULT_SEARCH_ENGINE = "duckduckgo"
ConsentCallback = Callable[[], str]
_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_BING_RESULT_RE = re.compile(
    r'<li class="b_algo".*?<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def resolve_cdp_url(raw: str | None = None) -> str | dict[str, str]:
    value = (raw if raw is not None else os.getenv("WYCKOFF_BROWSER_CDP_URL", DEFAULT_CDP_URL)).strip()
    if not value:
        return {"error": "CDP URL 不能为空"}
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return {"error": "CDP 只允许 http/https"}
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        return {"error": "CDP 只允许连接本机 127.0.0.1 / localhost"}
    if parsed.username or parsed.password:
        return {"error": "CDP URL 禁止携带凭据"}
    return value.rstrip("/")


def missing_playwright_message() -> str:
    return (
        f"当前解释器未安装 playwright（{sys.executable}）。"
        "请执行: wyckoff update"
        "（会安装 youngcan-wyckoff-analysis[browser] 与 Chromium）。"
        f"手动: uv pip install --python {sys.executable} 'youngcan-wyckoff-analysis[browser]' "
        f"&& {sys.executable} -m playwright install chromium"
    )


def chrome_cdp_profile_dir() -> str:
    return os.path.expanduser("~/.wyckoff/chrome-cdp")


def chrome_cdp_binary() -> str | None:
    system = platform.system()
    if system == "Darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        return path if os.path.isfile(path) else None
    if system == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return shutil.which("chrome") or shutil.which("chrome.exe")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def chrome_cdp_launch_argv(cdp_url: str | None = None) -> list[str] | dict[str, str]:
    resolved = resolve_cdp_url(cdp_url)
    if isinstance(resolved, dict):
        return resolved
    binary = chrome_cdp_binary()
    if not binary:
        return {"error": "未找到本机 Google Chrome / Chromium，请先安装浏览器"}
    port = urlparse(resolved).port or 9222
    profile = chrome_cdp_profile_dir()
    os.makedirs(profile, exist_ok=True)
    return [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def chrome_cdp_launch_hint(cdp_url: str | None = None) -> str:
    argv = chrome_cdp_launch_argv(cdp_url)
    if isinstance(argv, dict):
        return str(argv.get("error") or "无法生成 Chrome CDP 启动命令")
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


def launch_chrome_for_cdp(cdp_url: str | None = None) -> dict[str, Any]:
    resolved = resolve_cdp_url(cdp_url)
    endpoint = resolved if isinstance(resolved, str) else DEFAULT_CDP_URL
    argv = chrome_cdp_launch_argv(cdp_url)
    if isinstance(argv, dict):
        return {"ok": False, "cdp_url": endpoint, **argv}
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "cdp_url": endpoint,
            "error": f"启动 Chrome CDP 失败: {exc}",
            "hint": chrome_cdp_launch_hint(cdp_url),
        }
    return {
        "ok": True,
        "pid": proc.pid,
        "cdp_url": endpoint,
        "profile": chrome_cdp_profile_dir(),
        "argv": argv,
    }


def wait_for_cdp(
    cdp_url: str | None = None,
    *,
    timeout_seconds: float = CDP_WAIT_SECONDS,
    poll_interval: float = CDP_POLL_INTERVAL,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    last: dict[str, Any] = {"ok": False, "error": "等待 CDP 超时"}
    while time.monotonic() < deadline:
        last = browser_cdp_status(cdp_url)
        if last.get("ok"):
            return last
        err = str(last.get("error") or "")
        if "未安装 playwright" in err or "wyckoff update" in err:
            return last
        time.sleep(max(0.1, float(poll_interval)))
    last.setdefault("error", "等待 CDP 超时")
    return last


def ensure_cdp_session(
    consent_cb: ConsentCallback | None = None,
    *,
    cdp_url: str | None = None,
) -> dict[str, Any]:
    """Ensure local Chrome CDP is reachable; optionally ask consent then auto-launch."""
    status = browser_cdp_status(cdp_url)
    if status.get("ok"):
        return status
    err = str(status.get("error") or "")
    if "未安装 playwright" in err or ("wyckoff update" in err and "playwright" in err.lower()):
        return status
    if consent_cb is None:
        return {
            **status,
            "error": "本机 Chrome CDP 未就绪。TUI 中再次触发浏览器搜索可授权自动拉起，或执行 /browser start",
            "hint": status.get("hint") or chrome_cdp_launch_hint(cdp_url),
        }
    decision = (consent_cb() or "deny").strip().lower()
    if decision in {"deny", "cancel", "no"}:
        return {
            "ok": False,
            "cdp_url": status.get("cdp_url") or DEFAULT_CDP_URL,
            "error": "用户拒绝启动本机调试 Chrome，已取消浏览器搜索",
            "hint": "/browser start 可稍后手动授权启动",
        }
    if decision == "timeout":
        return {
            "ok": False,
            "cdp_url": status.get("cdp_url") or DEFAULT_CDP_URL,
            "error": "等待用户授权启动 Chrome CDP 超时",
            "hint": "/browser start 可稍后手动授权启动",
        }
    if decision not in {"allow", "once", "always", "yes", "ok"}:
        return {
            "ok": False,
            "cdp_url": status.get("cdp_url") or DEFAULT_CDP_URL,
            "error": f"未知授权结果: {decision}",
            "hint": chrome_cdp_launch_hint(cdp_url),
        }
    launched = launch_chrome_for_cdp(cdp_url)
    if not launched.get("ok"):
        return launched
    ready = wait_for_cdp(cdp_url)
    if ready.get("ok"):
        return {**ready, "launched": True, "pid": launched.get("pid")}
    return {
        "ok": False,
        "cdp_url": ready.get("cdp_url") or status.get("cdp_url") or DEFAULT_CDP_URL,
        "error": f"已尝试启动 Chrome，但仍无法连接 CDP: {ready.get('error') or 'unknown'}",
        "hint": ready.get("hint") or chrome_cdp_launch_hint(cdp_url),
        "pid": launched.get("pid"),
    }


def browser_cdp_status(cdp_url: str | None = None) -> dict[str, Any]:
    resolved = resolve_cdp_url(cdp_url)
    if isinstance(resolved, dict):
        return {"ok": False, "cdp_url": cdp_url or DEFAULT_CDP_URL, **resolved}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "cdp_url": resolved,
            "error": missing_playwright_message(),
            "hint": chrome_cdp_launch_hint(resolved),
        }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(resolved, timeout=5_000)
            version = getattr(browser, "version", "") or "Chrome"
            browser.close()
            return {"ok": True, "cdp_url": resolved, "browser": version}
    except Exception as exc:
        return {
            "ok": False,
            "cdp_url": resolved,
            "error": f"playwright 已安装，但无法连接本机 Chrome CDP: {exc}",
            "hint": chrome_cdp_launch_hint(resolved),
        }


def search_engine_url(query: str, engine: str | None = None) -> str | dict[str, str]:
    key = (engine or os.getenv("WYCKOFF_BROWSER_SEARCH_ENGINE", DEFAULT_SEARCH_ENGINE)).strip().lower()
    template = SEARCH_ENGINES.get(key)
    if not template:
        return {"error": f"不支持的搜索引擎: {key}（可选: {', '.join(SEARCH_ENGINES)}）"}
    return template.format(query=quote_plus(query.strip()))


def parse_search_results(html: str, *, max_results: int, engine: str | None = None) -> list[dict[str, str]]:
    key = (engine or os.getenv("WYCKOFF_BROWSER_SEARCH_ENGINE", DEFAULT_SEARCH_ENGINE)).strip().lower()
    pattern = _BING_RESULT_RE if key == "bing" else _DDG_RESULT_RE
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in pattern.finditer(html or ""):
        href = _unwrap_search_redirect(_clean_text(match.group(1)))
        title = _clean_text(match.group(2))
        safe = validate_public_http_url(href)
        if isinstance(safe, dict) or safe in seen:
            continue
        seen.add(safe)
        results.append({"title": title or safe, "url": safe, "snippet": ""})
        if len(results) >= max_results:
            break
    return results


def _unwrap_search_redirect(href: str) -> str:
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return href


def extract_readable_text(html: str, *, limit: int = MAX_PAGE_CHARS) -> str:
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return redact_sensitive_text(_WS_RE.sub(" ", text).strip()[: max(int(limit), 1)])


def research_via_cdp(
    query: str,
    *,
    max_results: int = 5,
    max_pages: int = 3,
    cdp_url: str | None = None,
) -> dict[str, Any]:
    q = query.strip()
    if not q:
        return {"error": "query 不能为空"}
    resolved = resolve_cdp_url(cdp_url)
    if isinstance(resolved, dict):
        return resolved
    search_url = search_engine_url(q)
    if isinstance(search_url, dict):
        return search_url
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "error": missing_playwright_message(),
            "hint": chrome_cdp_launch_hint(resolved),
        }
    n_results = max(1, min(int(max_results or 5), 10))
    n_pages = max(0, min(int(max_pages or 3), 5))
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(resolved, timeout=8_000)
            try:
                return _research_with_browser(
                    browser,
                    query=q,
                    search_url=search_url,
                    max_results=n_results,
                    max_pages=n_pages,
                    errors=errors,
                )
            finally:
                browser.close()
    except Exception as exc:
        return {
            "error": f"playwright 已安装，但无法连接本机 Chrome CDP: {exc}",
            "cdp_url": resolved,
            "hint": chrome_cdp_launch_hint(resolved),
        }


def _research_with_browser(
    browser: Any,
    *,
    query: str,
    search_url: str,
    max_results: int,
    max_pages: int,
    errors: list[str],
) -> dict[str, Any]:
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
        html = page.content()
        results = parse_search_results(html, max_results=max_results)
        if not results:
            errors.append("搜索页未解析到结果（可能被验证码拦截，请在已开的 Chrome 里完成验证后重试）")
        pages = _open_result_pages(context, results[:max_pages], errors)
        return {"query": query, "search_url": search_url, "results": results, "pages": pages, "errors": errors}
    finally:
        page.close()


def _open_result_pages(context: Any, results: list[dict[str, str]], errors: list[str]) -> list[dict[str, str]]:
    pages: list[dict[str, str]] = []
    for item in results:
        url = item["url"]
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            final_url = page.url
            safe = validate_public_http_url(final_url)
            if isinstance(safe, dict):
                errors.append(f"{url}: {safe.get('error', '导航目标不安全')}")
                continue
            pages.append(
                {
                    "url": safe,
                    "title": (page.title() or item.get("title") or "")[:200],
                    "content": extract_readable_text(page.content()),
                }
            )
        except Exception as exc:
            errors.append(f"{url}: {exc}")
        finally:
            page.close()
    return pages


def _clean_text(value: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value or "")).strip()
