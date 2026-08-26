"""Adapters for THS "今天炒什么" event themes."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from core.candidate_metadata import code6 as _code6
from core.concept_filters import is_actionable_theme_name, is_user_facing_etf
from core.theme_radar import normalize_theme_name
from utils.safe import safe_float as _as_float

logger = logging.getLogger(__name__)

DATA_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
THS_HOT_EVENTS_CACHE = DATA_CACHE_DIR / "ths_hot_events_cache.json"
THS_HOT_BASE = "https://news.10jqka.com.cn/app/concept_v2_api/open/api"
THS_HOT_REFERER = "https://news.10jqka.com.cn/app/hot_concept/v2/main"
THS_HOT_TTL_SECONDS = 30 * 60
THS_TIMEOUT = 15
THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Referer": THS_HOT_REFERER,
}


def fetch_ths_hot_events(
    *,
    date_cursor: str | None = None,
    detail_limit: int = 12,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch current THS event themes and normalize event details.

    THS uses ``date`` as a pagination cursor instead of a strict historical date.
    Use cache only for the default latest page to avoid mixing cursor semantics.
    """
    if not date_cursor and use_cache:
        cached = _read_cache()
        if cached:
            return cached
    session = requests.Session()
    page = _event_page(session, date_cursor)
    event_list = list(page.get("eventList") or [])
    quote_map = _event_quote_map(session, [str(x.get("eventId") or "") for x in event_list])
    events = [_normalize_event(item, quote_map.get(str(item.get("eventId") or ""))) for item in event_list]
    for event in events:
        event["trade_date"] = str(page.get("date") or "")
    for event in events[: max(int(detail_limit), 0)]:
        _attach_event_detail(session, event)
    snapshot = {"trade_date": str(page.get("date") or ""), "events": events, "source": "ths_hot_concept"}
    if not date_cursor and use_cache:
        _write_cache(snapshot)
    return snapshot


def ths_hot_events_to_concept_heat(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, event in enumerate(snapshot.get("events") or [], start=1):
        base = _event_heat_row(event, rank)
        if base:
            rows.append(base)
        for theme in _event_theme_rows(event, rank):
            rows.append(theme)
    return _dedupe_heat_rows(rows)


def summarize_ths_hot_events(snapshot: dict[str, Any], *, limit: int = 6) -> str:
    events = [
        x
        for x in snapshot.get("events") or []
        if x.get("theme") and not is_user_facing_etf(name=str(x.get("theme") or ""))
    ]
    if not events:
        return ""
    parts = []
    for item in events[:limit]:
        pct = _as_float(item.get("rise_pct"))
        heat = _heat_text(_as_float(item.get("heat")))
        limit_up = int(_as_float(item.get("limit_up_count")))
        parts.append(f"{item['theme']}({pct:+.1f}%, 热度{heat}, 涨停{limit_up})")
    return "；".join(parts)


def merge_concept_heat(base: list[dict[str, Any]], event_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*(event_rows or []), *(base or [])]:
        name = str(row.get("name") or "").strip()
        key = normalize_theme_name(name) or name
        if not key:
            continue
        current = merged.get(key)
        if current is None or _heat_sort_score(row) > _heat_sort_score(current):
            merged[key] = _merge_heat_row(row, current)
        elif current is not None:
            merged[key] = _merge_heat_row(current, row)
    return sorted(merged.values(), key=_heat_sort_score, reverse=True)


def _event_page(session: requests.Session, date_cursor: str | None) -> dict[str, Any]:
    params = {"date": date_cursor} if date_cursor else None
    payload = _get_json(session, "/concept/event/jtcsm/v1/event/list", params=params)
    rows = payload.get("data") or []
    return rows[0] if rows else {"date": "", "eventList": []}


def _event_quote_map(session: requests.Session, event_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [event_id for event_id in event_ids if event_id]
    if not ids:
        return {}
    payload = _post_json(session, "/concept/event/jtcsm/v1/event/quote", {"eventIds": ids})
    return {str(row.get("eventId") or ""): row for row in payload.get("data") or [] if row.get("eventId")}


def _attach_event_detail(session: requests.Session, event: dict[str, Any]) -> None:
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return
    detail = _get_json(session, "/concept/event/jtcsm/v1/event/detail", params={"eventId": event_id}).get("data") or {}
    table = _get_json(session, "/concept/event/jtcsm/v1/theme/table", params={"eventId": event_id}).get("data") or []
    event["summary"] = str(detail.get("summary") or event.get("summary") or "").strip()
    event["summary_items"] = [str(x).strip() for x in detail.get("summaryItems") or [] if str(x).strip()]
    event["jump_url"] = str(detail.get("jumpUrl") or "").strip()
    event["themes"] = [_normalize_theme(row) for row in (detail.get("themes") or event.get("themes") or [])]
    event["theme_table"] = [_normalize_theme_table(row) for row in table if isinstance(row, dict)]
    event["stocks"] = _dedupe_stocks([*event.get("stocks", []), *_stocks_from_theme_table(event["theme_table"])])


def _normalize_event(item: dict[str, Any], quote: dict[str, Any] | None) -> dict[str, Any]:
    themes = [_normalize_theme(row) for row in item.get("themes") or []]
    theme = _event_theme_name(item, themes)
    return {
        "event_id": str(item.get("eventId") or "").strip(),
        "trade_date": "",
        "title": str(item.get("title") or "").strip(),
        "theme": theme,
        "investment_direction": str(item.get("investmentDirection") or theme).strip(),
        "heat": _as_float(item.get("heat")),
        "rise_pct": _as_float((quote or {}).get("risePercent")),
        "limit_up_count": int(_as_float((quote or {}).get("limitUpCount"))),
        "themes": themes,
        "stocks": _dedupe_stocks([_normalize_stock(row) for row in item.get("topStocks") or []]),
        "source": "ths_hot_event",
    }


def _normalize_theme(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("showName") or row.get("indexName") or "").strip()
    return {
        "id": str(row.get("id") or "").strip(),
        "name": name,
        "theme": normalize_theme_name(name),
        "type": str(row.get("type") or "").strip(),
        "index_code": str(row.get("indexCode") or "").strip(),
        "rise_pct": _as_float(row.get("risePercent")),
        "limit_up_count": int(_as_float(row.get("limitUpCount"))),
    }


def _normalize_theme_table(row: dict[str, Any]) -> dict[str, Any]:
    theme = _normalize_theme(row)
    theme["stocks"] = _dedupe_stocks([_normalize_stock(item) for item in row.get("topStocks") or []])
    theme["children"] = [_normalize_theme_table(item) for item in row.get("children") or [] if isinstance(item, dict)]
    return theme


def _normalize_stock(row: dict[str, Any]) -> dict[str, Any]:
    code = _code6(row.get("stockCode") or row.get("code") or row.get("symbol"))
    return {
        "code": code,
        "name": str(row.get("stockName") or row.get("name") or "").strip(),
        "rise_pct": _as_float(row.get("risePercent") or row.get("pct")),
        "limit_up_state": row.get("limitUpState"),
        "reason": str(row.get("reason") or "").strip(),
    }


def _stocks_from_theme_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stocks: list[dict[str, Any]] = []
    for row in rows:
        stocks.extend(row.get("stocks") or [])
        stocks.extend(_stocks_from_theme_table(row.get("children") or []))
    return stocks


def _event_theme_name(item: dict[str, Any], themes: list[dict[str, Any]]) -> str:
    direction = normalize_theme_name(str(item.get("investmentDirection") or ""))
    if direction and is_actionable_theme_name(direction):
        return direction
    for row in themes:
        theme = str(row.get("theme") or "").strip()
        if theme and is_actionable_theme_name(theme):
            return theme
    return ""


def _event_heat_row(event: dict[str, Any], rank: int) -> dict[str, Any]:
    theme = normalize_theme_name(str(event.get("theme") or ""))
    if not theme or not is_actionable_theme_name(theme):
        return {}
    return _heat_row(theme, event, rank, top_stocks=event.get("stocks") or [])


def _event_theme_rows(event: dict[str, Any], rank: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for theme in _flatten_themes(event.get("theme_table") or event.get("themes") or []):
        name = normalize_theme_name(str(theme.get("theme") or theme.get("name") or ""))
        if name and is_actionable_theme_name(name):
            rows.append(_heat_row(name, event, rank, theme=theme, top_stocks=theme.get("stocks") or []))
    return rows


def _flatten_themes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(row)
        out.extend(_flatten_themes(row.get("children") or []))
    return out


def _heat_row(
    name: str,
    event: dict[str, Any],
    rank: int,
    *,
    theme: dict[str, Any] | None = None,
    top_stocks: list[dict[str, Any]],
) -> dict[str, Any]:
    theme = theme or {}
    return {
        "name": name,
        "pct": _as_float(theme.get("rise_pct") or event.get("rise_pct")),
        "net_inflow": 0.0,
        "event_heat": _as_float(event.get("heat")),
        "rank": rank,
        "source": "ths_hot_event",
        "event_id": event.get("event_id"),
        "title": event.get("title"),
        "event_theme": event.get("theme"),
        "limit_up_count": int(_as_float(theme.get("limit_up_count") or event.get("limit_up_count"))),
        "top_stocks": [stock for stock in top_stocks if stock.get("code")],
    }


def _dedupe_heat_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = normalize_theme_name(str(row.get("name") or "")) or str(row.get("name") or "")
        if not key:
            continue
        current = out.get(key)
        if current is None or _heat_sort_score(row) > _heat_sort_score(current):
            out[key] = _merge_heat_row(row, current)
        else:
            out[key] = _merge_heat_row(current, row)
    return sorted(out.values(), key=_heat_sort_score, reverse=True)


def _merge_heat_row(primary: dict[str, Any], secondary: dict[str, Any] | None) -> dict[str, Any]:
    if not secondary:
        return dict(primary)
    merged = dict(primary)
    merged["top_stocks"] = _dedupe_stocks([*(primary.get("top_stocks") or []), *(secondary.get("top_stocks") or [])])
    return merged


def _dedupe_stocks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        if code and code not in out:
            out[code] = row
    return list(out.values())


def _heat_sort_score(row: dict[str, Any]) -> float:
    return (
        _as_float(row.get("event_heat"))
        + max(_as_float(row.get("pct")), 0.0) * 10_000
        + 1000 / max(_as_float(row.get("rank")), 1.0)
    )


def _get_json(session: requests.Session, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = session.get(f"{THS_HOT_BASE}{path}", headers=THS_HEADERS, params=params, timeout=THS_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _post_json(session: requests.Session, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = session.post(f"{THS_HOT_BASE}{path}", headers=THS_HEADERS, json=payload, timeout=THS_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _read_cache() -> dict[str, Any]:
    try:
        if THS_HOT_EVENTS_CACHE.exists() and time.time() - THS_HOT_EVENTS_CACHE.stat().st_mtime < THS_HOT_TTL_SECONDS:
            return json.loads(THS_HOT_EVENTS_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("ths hot events cache read failed: %s", exc)
    return {}


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        THS_HOT_EVENTS_CACHE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("ths hot events cache write failed: %s", exc)


def _heat_text(value: float) -> str:
    return f"{value / 10000:.1f}万" if value >= 10_000 else f"{value:.0f}"
