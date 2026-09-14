"""Global-market recommendation tracking storage adapter."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.constants import TABLE_RECOMMENDATION_TRACKING_HK, TABLE_RECOMMENDATION_TRACKING_US
from core.recommendation_payload import event_change_pct
from integrations.recommendation_tracking_common import (
    fetch_records_from_table,
    upsert_to_table,
)
from integrations.supabase_base import create_admin_client, is_admin_configured, require_server_write_context

logger = logging.getLogger(__name__)

MARKET_TABLE_MAP: dict[str, str] = {
    "us": TABLE_RECOMMENDATION_TRACKING_US,
    "hk": TABLE_RECOMMENDATION_TRACKING_HK,
}


def upsert_global_recommendations(
    recommend_date: int,
    candidates: list[dict[str, Any]],
    market: str,
) -> bool:
    table = resolve_global_table(market)
    if not is_admin_configured() or not candidates:
        return False
    require_server_write_context(f"upsert global recommendations {market}")
    try:
        client = create_admin_client()
        history = fetch_records_from_table(client, table, "code,recommend_date,initial_price,current_price")
        existing_quotes = _event_quotes_by_code_date(history)
        payload = [_global_recommendation_payload(row, recommend_date, existing_quotes) for row in candidates]
        payload = [row for row in payload if row]
        if payload:
            client.table(table).upsert(payload, on_conflict="code,recommend_date").execute()
        return True
    except Exception as exc:
        logger.warning("upsert_global(%s) failed: %s", market, exc)
        return False


def fetch_global_recommendation_tracking_records(
    client,
    market: str,
    select_expr: str = "*",
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    table = resolve_global_table(market)
    return fetch_records_from_table(client, table, select_expr, page_size=page_size)


def upsert_global_recommendation_tracking_updates(
    client,
    market: str,
    updates: list[dict[str, Any]],
    batch_size: int = 500,
) -> int:
    table = resolve_global_table(market)
    return upsert_to_table(client, table, updates, batch_size=batch_size)


def resolve_global_table(market: str) -> str:
    table = MARKET_TABLE_MAP.get(market.lower())
    if not table:
        raise ValueError(f"unsupported market: {market}, must be 'us' or 'hk'")
    return table


def _global_recommendation_payload(
    candidate: dict[str, Any],
    recommend_date: int,
    existing_quotes: dict[tuple[str, int], tuple[float, float]] | None = None,
) -> dict[str, Any] | None:
    code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
    if not code:
        return None
    day_price = _extract_price(candidate)
    old_initial, old_current = (existing_quotes or {}).get((code, recommend_date), (0.0, 0.0))
    initial_price = day_price if day_price > 0 else old_initial
    already_repriced = old_current > 0 and old_initial > 0 and abs(old_current - old_initial) > 1e-9
    current_price = old_current if already_repriced else (day_price if day_price > 0 else old_current)
    if current_price <= 0:
        current_price = initial_price
    change = event_change_pct(initial_price, current_price)
    return {
        "code": code,
        "name": str(candidate.get("name", "")).strip(),
        "recommend_reason": str(candidate.get("tag") or candidate.get("recommend_reason") or "").strip(),
        "recommend_date": recommend_date,
        "initial_price": initial_price,
        "current_price": current_price,
        "change_pct": 0.0 if change is None else change,
        "funnel_score": _extract_score(candidate),
        "is_ai_recommended": False,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _event_quotes_by_code_date(rows: list[dict[str, Any]]) -> dict[tuple[str, int], tuple[float, float]]:
    quotes: dict[tuple[str, int], tuple[float, float]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        try:
            recommend_date = int(row.get("recommend_date"))
            initial = float(row.get("initial_price") or 0.0)
            current = float(row.get("current_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if code:
            quotes[(code, recommend_date)] = (initial if initial > 0 else 0.0, current if current > 0 else 0.0)
    return quotes


def _extract_price(candidate: dict[str, Any]) -> float:
    for key in ("initial_price", "latest_close", "current_price", "close"):
        raw = candidate.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _extract_score(candidate: dict[str, Any]) -> float | None:
    for key in ("funnel_score", "score", "priority_score"):
        raw = candidate.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None
