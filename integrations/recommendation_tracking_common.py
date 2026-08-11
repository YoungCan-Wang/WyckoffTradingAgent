"""Shared helpers for recommendation tracking storage and price refresh."""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from utils.safe import safe_float


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    step = max(int(size), 1)
    return [items[i : i + step] for i in range(0, len(items), step)]


def parse_recommend_date(raw_value: Any) -> date | None:
    if raw_value is None:
        return None
    s = str(raw_value).strip()
    if not s:
        return None
    try:
        if len(s) == 8 and s.isdigit():
            return datetime.strptime(s, "%Y%m%d").date()
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def recommend_date_to_yyyymmdd(raw: Any) -> str:
    parsed = parse_recommend_date(raw)
    return "" if parsed is None else parsed.strftime("%Y%m%d")


def pick_close_on_or_before(sorted_trade_dates: list[str], target_yyyymmdd: str) -> str:
    if not sorted_trade_dates or not target_yyyymmdd:
        return ""
    index = bisect_right(sorted_trade_dates, target_yyyymmdd) - 1
    return "" if index < 0 else sorted_trade_dates[index]


def resolve_tickflow_quote_price(quote: dict[str, Any] | None) -> float:
    row = quote or {}
    for key in ("last_price", "close", "last", "price", "current"):
        value = safe_float(row.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def quote_trade_date_yyyymmdd(quote: dict[str, Any] | None) -> str:
    timestamp_ms = safe_float((quote or {}).get("timestamp"), 0.0)
    if timestamp_ms <= 0:
        return ""
    timestamp_s = timestamp_ms / 1000.0 if timestamp_ms > 10_000_000_000 else timestamp_ms
    try:
        return datetime.fromtimestamp(timestamp_s, UTC).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    except Exception:
        return ""


def close_map_from_tickflow_hist(hist: pd.DataFrame | None) -> dict[str, float]:
    if hist is None or hist.empty or not {"date", "close"}.issubset(hist.columns):
        return {}
    work = hist[["date", "close"]].copy()
    work["trade_date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y%m%d")
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["trade_date", "close"])
    work = work[work["close"] > 0]
    return {str(day): float(price) for day, price in zip(work["trade_date"], work["close"])}


def ohlc_map_from_tickflow_hist(hist: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    if hist is None or hist.empty or not {"date", "high", "low", "close"}.issubset(hist.columns):
        return {}
    work = hist[["date", "high", "low", "close"]].copy()
    work["trade_date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y%m%d")
    for col in ("high", "low", "close"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["trade_date", "high", "low", "close"])
    work = work[(work["high"] > 0) & (work["low"] > 0) & (work["close"] > 0)]
    return {
        str(row.trade_date): {"high": float(row.high), "low": float(row.low), "close": float(row.close)}
        for row in work.itertuples(index=False)
    }


def fetch_records_from_table(client, table: str, select_expr: str, page_size: int = 1000) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    start = 0
    page = max(min(int(page_size), 1000), 1)
    while True:
        resp = (
            client.table(table)
            .select(select_expr)
            .order("recommend_date", desc=False)
            .order("id", desc=False)
            .range(start, start + page - 1)
            .execute()
        )
        batch = resp.data or []
        records.extend(batch)
        if len(batch) < page:
            return records
        start += page


def _is_schema_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return "column" in text or "schema cache" in text or "could not find" in text


def upsert_to_table(
    client,
    table: str,
    updates: list[dict[str, Any]],
    batch_size: int = 500,
    optional_columns: tuple[str, ...] = (),
) -> int:
    """Upsert rows keyed by (code, recommend_date), dropping optional columns the table doesn't have yet.

    There is no migration system for Supabase in this repo, so newly added optional fields
    (e.g. stop_loss_sim_pct) may not exist on a given table until it's manually added. Falling
    back to a compatible payload keeps the whole batch from failing on a schema mismatch.
    """
    written = 0
    clean = [row for row in updates if row.get("code") and row.get("recommend_date")]
    for chunk in chunked(clean, max(min(int(batch_size), 1000), 1)):
        try:
            client.table(table).upsert(chunk, on_conflict="code,recommend_date").execute()
        except Exception as exc:
            if not optional_columns or not _is_schema_miss(exc):
                raise
            compatible = [{k: v for k, v in row.items() if k not in optional_columns} for row in chunk]
            client.table(table).upsert(compatible, on_conflict="code,recommend_date").execute()
        written += len(chunk)
    return written


def empty_tracking_refresh_summary() -> dict[str, Any]:
    return {
        "rows_total": 0,
        "rows_updated": 0,
        "rows_skipped": 0,
        "codes_total": 0,
        "codes_no_data": 0,
        "latest_trade_date": "",
    }


def fetch_tickflow_tracking_market_data(
    api_key: str,
    symbols: list[str],
    batch_size: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, pd.DataFrame]]:
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=api_key)
    quotes: dict[str, dict[str, Any]] = {}
    hist_map: dict[str, pd.DataFrame] = {}
    for chunk in chunked(symbols, batch_size):
        quotes.update(client.get_quotes(chunk))
        hist_map.update(client.get_klines_batch(chunk, period="1d", count=120, adjust="none"))
    return quotes, hist_map


def first_recommend_date_yyyymmdd(rows: list[dict[str, Any]]) -> str:
    dates = [day for day in (recommend_date_to_yyyymmdd(row.get("recommend_date")) for row in rows) if day]
    return min(dates) if dates else ""


def tracking_update_from_close_map(
    row: dict[str, Any],
    code: int | str,
    trade_dates: list[str],
    close_map: dict[str, float],
    current_close: float,
    now_iso: str,
    *,
    first_recommend_date: str = "",
) -> dict[str, Any] | None:
    recommend_date = recommend_date_to_yyyymmdd(row.get("recommend_date"))
    anchor_date = first_recommend_date or recommend_date
    pick_date = pick_close_on_or_before(trade_dates, anchor_date)
    initial_close = float(close_map.get(pick_date, 0.0)) if pick_date else 0.0
    if initial_close <= 0 or current_close <= 0:
        return None
    return {
        "id": row.get("id"),
        "code": code,
        "recommend_date": int(recommend_date) if recommend_date.isdigit() else None,
        "initial_price": round(initial_close, 4),
        "current_price": round(current_close, 4),
        "change_pct": round((current_close - initial_close) / initial_close * 100.0, 2),
        "updated_at": now_iso,
    }
