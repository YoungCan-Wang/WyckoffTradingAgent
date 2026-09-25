"""Fetch East Money stock news and fold it into chart overlay events."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from core.news_chart_events import events_as_dicts, select_news_chart_events

EASTMONEY_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_PAGE_SIZE = 20
_MAX_PAGES = 4
_TIMEOUT_SECONDS = 12


@dataclass
class NewsBatch:
    items: list[dict[str, Any]] = field(default_factory=list)
    pages_succeeded: int = 0
    failed_pages: list[dict[str, Any]] = field(default_factory=list)
    rejected_items: int = 0
    coverage_status: str = "unknown"

    @property
    def request_status(self) -> str:
        if not self.pages_succeeded:
            return "unavailable"
        return "partial" if self.failed_pages or self.rejected_items else "ok"


def fetch_eastmoney_news_batch(keyword: str, *, pages: int = 2) -> NewsBatch:
    """Observation collector; legacy list callers retain their strict/error contract."""
    result = NewsBatch()
    page_limit = min(max(int(pages), 1), _MAX_PAGES)
    for page in range(1, page_limit + 1):
        try:
            payload = _request_news_page(str(keyword).strip(), page)
            batch = payload.get("result", {}).get("cmsArticleWebOld") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise ValueError("Invalid East Money news payload")
        except Exception as exc:
            result.failed_pages.append({"page": page, "error": type(exc).__name__})
            continue
        result.pages_succeeded += 1
        for item in batch:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
                result.rejected_items += 1
                continue
            result.items.append(_normalize_article(item))
        if len(batch) < _PAGE_SIZE:
            break
        if page == page_limit:
            result.coverage_status = "possibly_truncated"
    return result


def load_news_chart_events(
    code: str,
    start: str,
    end: str,
    session_dates: list[str],
    *,
    limit: int = 8,
    name: str = "",
) -> list[dict[str, Any]]:
    symbol = str(code or "").strip()
    if not symbol.isdigit() or len(symbol) != 6:
        return []
    items = fetch_eastmoney_stock_news(symbol)
    return events_as_dicts(
        select_news_chart_events(
            items,
            start=start,
            end=end,
            session_dates=session_dates,
            limit=limit,
            symbol=symbol,
            name=name,
        )
    )


def fetch_eastmoney_stock_news(code: str, *, pages: int = _MAX_PAGES) -> list[dict[str, Any]]:
    return fetch_eastmoney_news(str(code or "").strip(), pages=pages)


def fetch_eastmoney_news(keyword: str, *, pages: int = _MAX_PAGES, strict: bool = False) -> list[dict[str, Any]]:
    query = str(keyword or "").strip()
    if not query:
        return []
    rows: list[dict[str, Any]] = []
    for page in range(1, max(int(pages), 1) + 1):
        payload = _request_news_page(query, page)
        batch = payload.get("result", {}).get("cmsArticleWebOld") if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            if strict:
                raise ValueError("Invalid East Money news payload")
            break
        if strict and any(not isinstance(item, dict) for item in batch):
            raise ValueError("Invalid East Money article")
        if not batch:
            break
        rows.extend(_normalize_article(item) for item in batch if isinstance(item, dict))
        if len(batch) < _PAGE_SIZE:
            break
    return rows


def _request_news_page(keyword: str, page: int) -> dict[str, Any]:
    params = urlencode(
        {
            "cb": "jQuery3510",
            "param": json.dumps(_search_param(keyword, page), ensure_ascii=False),
            "_": "1",
        }
    )
    request = Request(
        f"{EASTMONEY_NEWS_URL}?{params}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://so.eastmoney.com/news/s?keyword={quote(keyword, safe='')}",
        },
    )
    with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return _parse_jsonp(response.read().decode("utf-8", errors="replace"))


def _search_param(keyword: str, page: int) -> dict[str, Any]:
    return {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": page,
                "pageSize": _PAGE_SIZE,
                "preTag": "",
                "postTag": "",
            }
        },
    }


def _parse_jsonp(text: str) -> dict[str, Any]:
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        return {}
    payload = json.loads(text[start + 1 : end])
    return payload if isinstance(payload, dict) else {}


def _normalize_article(item: dict[str, Any]) -> dict[str, Any]:
    code = str(item.get("code") or "").strip()
    return {
        "title": str(item.get("title") or "").strip(),
        "content": str(item.get("content") or "").strip(),
        "published_at": str(item.get("date") or ""),
        "source": str(item.get("mediaName") or "eastmoney"),
        "url": f"https://finance.eastmoney.com/a/{code}.html" if code else "",
    }
