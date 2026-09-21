"""公司大事 / 停牌扫描运行时：只写观察产物并通知，不进漏斗或 OMS。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.corporate_event_scan import (
    CorporateEventHit,
    filter_recent_hits,
    render_corporate_event_report,
    scan_corporate_events,
)
from integrations.corporate_event_sources import collect_corporate_event_items
from utils.feishu import send_feishu_notification

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CorporateEventScanResult:
    as_of: str
    hits: list[CorporateEventHit]
    report: str
    markdown_path: Path
    json_path: Path
    source_ok: bool


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
    lookback_days: int = 2,
    output: str = "logs/corporate_event_scan.md",
    json_output: str = "logs/corporate_event_scan.json",
    persist: bool = True,
) -> CorporateEventScanResult:
    as_of_text = as_of or shanghai_now().strftime("%Y-%m-%d %H:%M")
    items, source_ok = _load_items(extra_items, fetch_news, fetch_cls)
    hits = filter_recent_hits(scan_corporate_events(items), as_of=as_of_text, lookback_days=lookback_days)
    report = render_corporate_event_report(hits, as_of=as_of_text)
    markdown_path = _write_text(output, report) if persist else Path()
    json_path = (
        _write_text(json_output, json.dumps(_payload(as_of_text, hits, source_ok), ensure_ascii=False, indent=2))
        if persist
        else Path()
    )
    return CorporateEventScanResult(as_of_text, hits, report, markdown_path, json_path, source_ok)


def notify_corporate_event_scan(
    result: CorporateEventScanResult,
    *,
    webhook: str | None = None,
    dry_run: bool = False,
) -> CorporateEventNotification:
    title = f"公司大事/停牌扫描 {result.as_of}"
    if dry_run:
        return CorporateEventNotification(False, True, title, "dry_run")
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip() if webhook is None else webhook.strip()
    if not webhook:
        return CorporateEventNotification(False, False, title, "FEISHU_WEBHOOK_URL 未配置")
    ok = bool(send_feishu_notification(webhook, title, result.report))
    return CorporateEventNotification(True, ok, title, "ok" if ok else "failed")


def _load_items(extra_items, fetch_news, fetch_cls) -> tuple[list[dict[str, Any]], bool]:
    try:
        items = collect_corporate_event_items(extra_items=extra_items, fetch_news=fetch_news, fetch_cls=fetch_cls)
        return items, True
    except Exception:
        return list(extra_items or []), False


def _payload(as_of: str, hits: list[CorporateEventHit], source_ok: bool) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "note": "已公告与媒体电报观察，不是实盘，也不是漏斗买许可。",
        "source_ok": source_ok,
        "hits": [hit.as_dict() for hit in hits],
    }


def _write_text(path: str, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
