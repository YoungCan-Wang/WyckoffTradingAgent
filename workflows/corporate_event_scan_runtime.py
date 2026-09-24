"""Corporate news observations and notifications, isolated from trading state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.corporate_event_scan import (
    SOURCE_NOTE,
    CorporateEventHit,
    filter_recent_hits,
    render_corporate_event_report,
    scan_corporate_events,
)
from core.corporate_event_time import SHANGHAI, event_time, observation_cutoff
from integrations.corporate_event_sources import SourceResult, collect_corporate_event_items
from utils.feishu import send_feishu_notification


@dataclass(frozen=True)
class CorporateEventScanResult:
    as_of: str
    hits: list[CorporateEventHit]
    report: str
    markdown_path: Path
    json_path: Path
    sources: list[SourceResult]
    source_status: str
    undated_hits: list[CorporateEventHit]

    @property
    def source_ok(self) -> bool:
        return self.source_status == "ok"

    def payload(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "timezone": "Asia/Shanghai",
            "lookback_hours": 48,
            "note": "已公告与媒体电报观察，不是实盘，也不是漏斗买许可。",
            "coverage": SOURCE_NOTE,
            "source_ok": self.source_ok,
            "source_status": self.source_status,
            "sources": [source.as_dict() for source in self.sources],
            "hits": [hit.as_dict() for hit in self.hits],
            "undated_hits": [hit.as_dict() for hit in self.undated_hits],
        }


@dataclass(frozen=True)
class CorporateEventNotification:
    attempted: bool
    ok: bool
    title: str
    reason: str


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)


def run_corporate_event_scan(
    *,
    as_of: str | None = None,
    extra_items: list[dict[str, Any]] | None = None,
    fetch_news=None,
    fetch_cls=None,
    output: str = "logs/corporate_event_scan.md",
    json_output: str = "logs/corporate_event_scan.json",
    persist: bool = True,
) -> CorporateEventScanResult:
    as_of_text = observation_cutoff(as_of or shanghai_now().isoformat()).isoformat(timespec="milliseconds")
    collection = collect_corporate_event_items(extra_items=extra_items, fetch_news=fetch_news, fetch_cls=fetch_cls)
    classified = scan_corporate_events(collection.items)
    hits = filter_recent_hits(classified, as_of=as_of_text)
    undated = [hit for hit in classified if event_time(hit.published_at) is None]
    report = render_corporate_event_report(
        hits,
        as_of=as_of_text,
        source_status=collection.status,
        failed_sources=tuple(source.source for source in collection.sources if not source.ok),
        undated_count=len(undated),
    )
    result = CorporateEventScanResult(
        as_of_text, hits, report, Path(output), Path(json_output), collection.sources, collection.status, undated
    )
    if persist:
        _write_text(result.markdown_path, report)
        _write_text(result.json_path, json.dumps(result.payload(), ensure_ascii=False, indent=2))
    return result


def notify_corporate_event_scan(
    result: CorporateEventScanResult, *, webhook: str | None = None, dry_run: bool = False
) -> CorporateEventNotification:
    suffix = "" if result.source_ok else "（数据源异常）"
    title = f"公司大事/停牌扫描 {result.as_of}{suffix}"
    if dry_run:
        return CorporateEventNotification(False, True, title, "dry_run")
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip() if webhook is None else webhook.strip()
    if not webhook:
        return CorporateEventNotification(False, False, title, "FEISHU_WEBHOOK_URL 未配置")
    ok = bool(send_feishu_notification(webhook, title, result.report))
    return CorporateEventNotification(True, ok, title, "ok" if ok else "failed")


def _write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
