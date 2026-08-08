"""Global-market recommendation tracking storage adapter."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.constants import TABLE_RECOMMENDATION_TRACKING_HK, TABLE_RECOMMENDATION_TRACKING_US
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
        history = fetch_records_from_table(client, table, "code,recommend_date,initial_price")
        first_prices = _first_prices_by_code(history)
        payload = [_global_recommendation_payload(row, recommend_date, first_prices) for row in candidates]
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
    first_prices: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
    if not code:
        return None
    current_price = _extract_price(candidate)
    sticky_price = float((first_prices or {}).get(code, 0.0) or 0.0)
    initial_price = sticky_price if sticky_price > 0 else current_price
    return {
        "code": code,
        "name": str(candidate.get("name", "")).strip(),
        "recommend_reason": str(candidate.get("tag") or candidate.get("recommend_reason") or "").strip(),
        "recommend_date": recommend_date,
        "initial_price": initial_price,
        "current_price": current_price,
        "change_pct": _change_pct(initial_price, current_price),
        "funnel_score": _extract_score(candidate),
        "is_ai_recommended": False,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _first_prices_by_code(rows: list[dict[str, Any]]) -> dict[str, float]:
    earliest: dict[str, tuple[int, float]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        try:
            recommend_date = int(row.get("recommend_date"))
            price = float(row.get("initial_price") or 0.0)
        except (TypeError, ValueError):
            continue
        previous = earliest.get(code)
        if code and (previous is None or recommend_date < previous[0]):
            earliest[code] = (recommend_date, price if price > 0 else 0.0)
        elif code and previous and recommend_date == previous[0] and price > 0:
            earliest[code] = (recommend_date, price)
    return {code: price for code, (_, price) in earliest.items() if price > 0}


def _change_pct(initial_price: float, current_price: float) -> float:
    if initial_price <= 0 or current_price <= 0:
        return 0.0
    return round((current_price - initial_price) / initial_price * 100.0, 2)


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
