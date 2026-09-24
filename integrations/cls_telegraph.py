"""财联社最新电报批次；来源失败交给采集层记录，不伪装成空结果。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.corporate_event_scan import normalize_stock_code
from core.corporate_event_time import SHANGHAI

CLS_TELEGRAPH_URL = "https://www.cls.cn/nodeapi/updateTelegraphList"
_TIMEOUT_SECONDS = 8


def fetch_cls_telegraphs(*, timeout: int = _TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    payload = _request_cls(timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("roll_data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Invalid CLS telegraph payload")
    if any(not isinstance(item, dict) for item in rows):
        raise ValueError("Invalid CLS telegraph row")
    return [_normalize_cls(item) for item in rows]


def _request_cls(timeout: int) -> dict[str, Any]:
    params = urlencode({"app": "CailianpressWeb", "os": "web", "sv": "8.4.6"})
    request = Request(
        f"{CLS_TELEGRAPH_URL}?{params}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/telegraph"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, dict) else {}


def _normalize_cls(item: dict[str, Any]) -> dict[str, Any]:
    first = _first_stock(item)
    title = str(item.get("title") or item.get("brief") or "").strip()
    content = str(item.get("content") or item.get("descr") or "").strip()
    return {
        "title": title or content[:80],
        "content": content,
        "code": _stock_code(first),
        "name": str(first.get("name") or first.get("secu_name") or "").strip(),
        "source": "财联社",
        "published_at": _cls_time(item.get("ctime") or item.get("time")),
        "url": str(item.get("shareurl") or item.get("url") or ""),
    }


def _first_stock(item: dict[str, Any]) -> dict[str, Any]:
    rows = item.get("stock_list") or item.get("stocks") or []
    return rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}


def _stock_code(stock: dict[str, Any]) -> str:
    return normalize_stock_code(stock.get("StockID") or stock.get("code"))


def _cls_time(raw: Any) -> str:
    try:
        stamp = int(raw)
    except (TypeError, ValueError):
        return str(raw or "")
    if stamp > 10**12:
        stamp //= 1000
    try:
        return datetime.fromtimestamp(stamp, tz=UTC).astimezone(SHANGHAI).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""
