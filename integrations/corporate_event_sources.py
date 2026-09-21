"""公司大事扫描取数：东财关键词 + 财联社电报，不读 suspend_d，不写实盘。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.corporate_event_scan import SEARCH_KEYWORDS

logger = logging.getLogger(__name__)

NewsFetcher = Callable[..., list[dict[str, Any]]]


def collect_corporate_event_items(
    *,
    extra_items: list[dict[str, Any]] | None = None,
    fetch_news: NewsFetcher | None = None,
    fetch_cls: NewsFetcher | None = None,
    pages: int = 2,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(_eastmoney_keyword_items(fetch_news, pages))
    items.extend(_cls_items(fetch_cls))
    if extra_items:
        items.extend(extra_items)
    return items


def _eastmoney_keyword_items(fetch_news: NewsFetcher | None, pages: int) -> list[dict[str, Any]]:
    if fetch_news is None:
        from integrations.stock_news_events import fetch_eastmoney_news

        fetch_news = fetch_eastmoney_news
    rows: list[dict[str, Any]] = []
    for keyword in SEARCH_KEYWORDS:
        try:
            rows.extend(fetch_news(keyword, pages=pages))
        except Exception:
            logger.warning("eastmoney keyword fetch failed: %s", keyword, exc_info=True)
    return rows


def _cls_items(fetch_cls: NewsFetcher | None) -> list[dict[str, Any]]:
    if fetch_cls is None:
        from integrations.cls_telegraph import fetch_cls_telegraphs

        fetch_cls = fetch_cls_telegraphs
    try:
        return list(fetch_cls())
    except Exception:
        logger.warning("cls telegraph collect failed", exc_info=True)
        return []
