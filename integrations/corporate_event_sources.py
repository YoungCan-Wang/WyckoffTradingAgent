"""Bounded news collection with explicit per-source failure provenance."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any

from core.corporate_event_scan import SEARCH_KEYWORDS
from core.corporate_event_time import event_time

logger = logging.getLogger(__name__)
NewsFetcher = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class SourceResult:
    source: str
    ok: bool
    item_count: int
    error: str = ""
    oldest_at: str = ""
    newest_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CorporateEventCollection:
    items: list[dict[str, Any]]
    sources: list[SourceResult]

    @property
    def status(self) -> str:
        successes = sum(source.ok for source in self.sources)
        return "unavailable" if not successes else "ok" if successes == len(self.sources) else "partial"


def collect_corporate_event_items(
    *,
    extra_items: list[dict[str, Any]] | None = None,
    fetch_news: NewsFetcher | None = None,
    fetch_cls: NewsFetcher | None = None,
    pages: int = 2,
) -> CorporateEventCollection:
    if fetch_news is None:
        from integrations.stock_news_events import fetch_eastmoney_news

        fetch_news = partial(fetch_eastmoney_news, strict=True)
    if fetch_cls is None:
        from integrations.cls_telegraph import fetch_cls_telegraphs

        fetch_cls = fetch_cls_telegraphs
    jobs = [(f"eastmoney:{keyword}", partial(fetch_news, keyword, pages=pages)) for keyword in SEARCH_KEYWORDS]
    jobs.append(("cls", fetch_cls))
    # Each source has its own network deadline. Concurrent collection keeps a slow
    # keyword from consuming the entire interactive tool deadline.
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_collect_source, jobs))
    items = [item for batch, _ in results for item in batch]
    return CorporateEventCollection(items + list(extra_items or []), [source for _, source in results])


def _collect_source(job: tuple[str, NewsFetcher]) -> tuple[list[dict[str, Any]], SourceResult]:
    source, fetcher = job
    try:
        items = fetcher()
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Invalid news item list")
        stamps = sorted(
            stamp
            for item in items
            if (stamp := event_time(str(item.get("published_at") or item.get("date") or ""))) is not None
        )
        return items, SourceResult(
            source,
            True,
            len(items),
            oldest_at=stamps[0].isoformat() if stamps else "",
            newest_at=stamps[-1].isoformat() if stamps else "",
        )
    except Exception as exc:
        logger.warning("corporate event source %s unavailable (%s)", source, type(exc).__name__)
        return [], SourceResult(source, False, 0, error=type(exc).__name__)
