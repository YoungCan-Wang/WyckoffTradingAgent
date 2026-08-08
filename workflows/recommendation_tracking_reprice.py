"""Recommendation tracking price-refresh workflows."""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from core.candidate_metadata import code6
from integrations.recommendation_global import (
    fetch_global_recommendation_tracking_records,
    upsert_global_recommendation_tracking_updates,
)
from integrations.recommendation_tracking_common import (
    close_map_from_tickflow_hist,
    empty_tracking_refresh_summary,
    fetch_tickflow_tracking_market_data,
    first_recommend_date_yyyymmdd,
    parse_recommend_date,
    quote_trade_date_yyyymmdd,
    recommend_date_to_yyyymmdd,
    resolve_tickflow_quote_price,
    tracking_update_from_close_map,
)
from integrations.supabase_base import create_admin_client, is_admin_configured, require_server_write_context
from integrations.supabase_recommendation import (
    fetch_recommendation_tracking_records,
    upsert_recommendation_tracking_price_updates,
    upsert_recommendation_tracking_updates,
)
from utils.env import env_flag

logger = logging.getLogger(__name__)


def _build_tickflow_tracking_updates(
    grouped: dict[str, list[dict[str, Any]]],
    quotes: dict[str, dict[str, Any]],
    hist_map: dict[str, pd.DataFrame],
    now_iso: str,
) -> tuple[list[dict[str, Any]], int, str]:
    from integrations.tickflow_client import normalize_cn_symbol

    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_trade_date = ""
    for code, rows in grouped.items():
        sym = normalize_cn_symbol(code)
        quote = quotes.get(sym) or {}
        current_price = resolve_tickflow_quote_price(quote)
        latest_trade_date = max(latest_trade_date, quote_trade_date_yyyymmdd(quote))
        close_map = close_map_from_tickflow_hist(hist_map.get(sym))
        trade_dates = sorted(close_map)
        if current_price <= 0 and trade_dates:
            current_price = float(close_map[trade_dates[-1]])
        latest_trade_date = max(latest_trade_date, trade_dates[-1] if trade_dates else "")
        if current_price <= 0 or not trade_dates:
            codes_no_data += 1
            continue
        first_date = first_recommend_date_yyyymmdd(rows)
        for row in rows:
            update = _tickflow_tracking_update(row, code, trade_dates, close_map, current_price, now_iso, first_date)
            if update is not None:
                updates.append(update)
    return updates, codes_no_data, latest_trade_date


def _tickflow_tracking_update(
    row: dict[str, Any],
    code: str,
    trade_dates: list[str],
    close_map: dict[str, float],
    current_price: float,
    now_iso: str,
    first_recommend_date: str = "",
) -> dict[str, Any] | None:
    update = tracking_update_from_close_map(
        row,
        int(code),
        trade_dates,
        close_map,
        current_price,
        now_iso,
        first_recommend_date=first_recommend_date,
    )
    return update


def _parse_write_date(record: dict[str, Any]) -> date | None:
    rec_date = parse_recommend_date(record.get("recommend_date"))
    if rec_date is not None:
        return rec_date
    created = record.get("created_at")
    if created is None or not str(created).strip():
        return None
    try:
        value = str(created).strip()
        if "T" in value or " " in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").date()
        return datetime.fromisoformat(value).date()
    except Exception:
        logger.debug("failed to parse created_at date: %s", created, exc_info=True)
        return None


def _resolve_initial_price_from_history(code: str, rec_date: date) -> float:
    try:
        from integrations.data_source import fetch_stock_hist

        rec_s = rec_date.strftime("%Y-%m-%d")
        hist = fetch_stock_hist(code, rec_s, rec_s, adjust="qfq")
        price = _last_valid_close(hist)
        if price > 0:
            return price
        start_s = (rec_date - timedelta(days=7)).strftime("%Y-%m-%d")
        hist = fetch_stock_hist(code, start_s, rec_s, adjust="qfq")
        return _last_close_on_or_before(hist, rec_date)
    except Exception:
        return 0.0


def _last_valid_close(hist: pd.DataFrame | None) -> float:
    if hist is None or hist.empty or "收盘" not in hist.columns:
        return 0.0
    close_s = pd.to_numeric(hist.get("收盘"), errors="coerce").dropna()
    price = float(close_s.iloc[-1]) if not close_s.empty else 0.0
    return price if price > 0 else 0.0


def _last_close_on_or_before(hist: pd.DataFrame | None, rec_date: date) -> float:
    if hist is None or hist.empty or not {"日期", "收盘"}.issubset(hist.columns):
        return 0.0
    work = hist.copy()
    work["日期"] = pd.to_datetime(work["日期"], errors="coerce")
    work["收盘"] = pd.to_numeric(work["收盘"], errors="coerce")
    work = work.dropna(subset=["日期", "收盘"]).sort_values("日期")
    work = work[work["日期"].dt.date <= rec_date]
    if work.empty:
        return 0.0
    price = float(work["收盘"].iloc[-1])
    return price if price > 0 else 0.0


def _resolve_price(code: str, price_map, history_fn, spot_fn) -> float | None:
    if price_map:
        try:
            price = float(price_map.get(code) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return price
    price = history_fn(code)
    return price if price is not None else spot_fn(code)


def _build_price_update_row(
    record: dict,
    new_price: float,
    code: str,
    now_iso: str,
    *,
    first_recommend_date: date | None = None,
) -> dict:
    row: dict = {"id": record["id"], "current_price": new_price, "updated_at": now_iso}
    initial_price = float(record.get("initial_price") or 0.0)
    if initial_price > 0:
        row["change_pct"] = round((new_price - initial_price) / initial_price * 100.0, 2)
        return row
    anchor = first_recommend_date or parse_recommend_date(record.get("recommend_date"))
    backfill = _resolve_initial_price_from_history(code, anchor) if anchor else 0.0
    row["initial_price"] = backfill if backfill > 0 else new_price
    row["change_pct"] = round((new_price - row["initial_price"]) / row["initial_price"] * 100.0, 2)
    return row


def _group_records_by_int_code(records: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        try:
            code_int = int(record.get("code"))
        except (TypeError, ValueError):
            continue
        grouped.setdefault(code_int, []).append(record)
    return grouped


def _group_records_by_code6(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        code = code6(record.get("code"))
        if code:
            grouped.setdefault(code, []).append(record)
    return grouped


def _resolve_tracking_history_window() -> tuple[str | None, str | None]:
    try:
        from integrations.fetch_a_share_csv import resolve_trading_window
        from utils.trading_clock import resolve_end_calendar_day

        window = resolve_trading_window(end_calendar_day=resolve_end_calendar_day(), trading_days=20)
        return window.start_trade_date.strftime("%Y-%m-%d"), window.end_trade_date.strftime("%Y-%m-%d")
    except Exception:
        return None, None


def _make_history_price_resolver(hist_start: str | None, hist_end: str | None):
    cache: dict[str, float] = {}

    def _price_from_history(code: str) -> float | None:
        if code in cache:
            return cache[code] if cache[code] > 0 else None
        if not hist_start or not hist_end:
            cache[code] = 0.0
            return None
        try:
            from integrations.data_source import fetch_stock_hist

            cache[code] = _last_valid_close(fetch_stock_hist(code, hist_start, hist_end, adjust="qfq"))
            return cache[code] if cache[code] > 0 else None
        except Exception:
            cache[code] = 0.0
            return None

    return _price_from_history


def _spot_price_resolver(allow_spot_fallback: bool):
    def _price_from_spot(code: str) -> float | None:
        if not allow_spot_fallback:
            return None
        try:
            from integrations.spot_snapshot import fetch_stock_spot_snapshot

            snapshot = fetch_stock_spot_snapshot(code, force_refresh=False)
            price = float(snapshot["close"]) if snapshot and snapshot.get("close") is not None else 0.0
            return price if price > 0 else None
        except Exception:
            return None

    return _price_from_spot


def _build_current_price_updates(
    unique_codes: list[int],
    records_by_code: dict[int, list[dict[str, Any]]],
    price_map: dict[str, float] | None,
    history_fn,
    spot_fn,
    now_iso: str,
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for code_int in unique_codes:
        code = f"{code_int:06d}"
        current_price = _resolve_price(code, price_map, history_fn, spot_fn)
        if current_price is None:
            continue
        rows = records_by_code.get(code_int, [])
        first_date = min(
            (d for d in (parse_recommend_date(row.get("recommend_date")) for row in rows) if d is not None),
            default=None,
        )
        for record in rows:
            updates.append(
                _build_price_update_row(record, current_price, code, now_iso, first_recommend_date=first_date)
            )
    return updates


def sync_all_tracking_prices(price_map: dict[str, float] | None = None) -> int:
    if not is_admin_configured():
        logger.info("sync_all_tracking_prices skipped: Supabase is not configured")
        return 0
    require_server_write_context("sync recommendation_tracking prices")
    try:
        client = create_admin_client()
        records = fetch_recommendation_tracking_records(client, "*")
        if not records:
            logger.info("sync_all_tracking_prices skipped: recommendation table is empty")
            return 0
        unique_codes = sorted({int(row["code"]) for row in records if row.get("code") is not None})
        history_fn = _make_history_price_resolver(*_resolve_tracking_history_window())
        spot_fn = _spot_price_resolver(_spot_fallback_enabled())
        updates = _build_current_price_updates(
            unique_codes,
            _group_records_by_int_code(records),
            price_map,
            history_fn,
            spot_fn,
            datetime.now(UTC).isoformat(),
        )
        return upsert_recommendation_tracking_price_updates(client, updates)
    except Exception as exc:
        logger.warning("sync_all_tracking_prices failed: %s", exc)
        return 0


def _spot_fallback_enabled() -> bool:
    return env_flag("RECOMMENDATION_PRICE_ALLOW_SPOT_FALLBACK")


def correct_tracking_initial_prices(*, apply: bool = True) -> dict[str, Any]:
    """按 code 首次 recommend_date 收盘价纠偏 initial_price。

    apply=False 时只预览将改写的行，不落库。
    """
    summary = {"rows_total": 0, "rows_changed": 0, "rows_written": 0, "samples": []}
    if not is_admin_configured():
        logger.info("correct_tracking_initial_prices skipped: Supabase is not configured")
        return summary
    if apply:
        require_server_write_context("correct recommendation_tracking prices")
    try:
        client = create_admin_client()
        records = fetch_recommendation_tracking_records(client, "*")
        summary["rows_total"] = len(records)
        first_dates = _first_recommend_dates_by_code(records)
        cache: dict[tuple[str, date], float] = {}
        updates = [
            update
            for record in records
            if (update := _correct_initial_price_update(record, cache, first_dates)) is not None
        ]
        summary["rows_changed"] = len(updates)
        summary["samples"] = updates[:10]
        if not apply or not updates:
            return summary
        summary["rows_written"] = upsert_recommendation_tracking_price_updates(client, updates)
        return summary
    except Exception as exc:
        logger.warning("correct_tracking_initial_prices failed: %s", exc)
        return summary


def _first_recommend_dates_by_code(records: list[dict[str, Any]]) -> dict[str, date]:
    first_dates: dict[str, date] = {}
    for record in records:
        if record.get("code") is None:
            continue
        code = f"{int(record['code']):06d}"
        write_date = _parse_write_date(record)
        if write_date is None:
            continue
        prev = first_dates.get(code)
        if prev is None or write_date < prev:
            first_dates[code] = write_date
    return first_dates


def _correct_initial_price_update(
    record: dict[str, Any],
    cache: dict[tuple[str, date], float],
    first_dates: dict[str, date],
) -> dict[str, Any] | None:
    if record.get("code") is None:
        return None
    current_price = float(record.get("current_price") or 0.0)
    if current_price <= 0:
        return None
    code = f"{int(record['code']):06d}"
    first_date = first_dates.get(code) or _parse_write_date(record)
    if first_date is None:
        return None
    key = (code, first_date)
    if key not in cache:
        cache[key] = _resolve_initial_price_from_history(code, first_date)
    raw_price = cache[key]
    if raw_price <= 0:
        return None
    # PG numeric 展示/存储通常到分；与前复权浮点直接比会反复误判为脏数据。
    initial_price = round(raw_price, 2)
    old_price = round(float(record.get("initial_price") or 0.0), 2)
    if abs(old_price - initial_price) < 1e-9:
        return None
    change_pct = round((current_price - initial_price) / initial_price * 100.0, 2)
    return {
        "id": record["id"],
        "code": int(record["code"]),
        "recommend_date": record.get("recommend_date"),
        "initial_price": initial_price,
        "change_pct": change_pct,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _to_ts_code(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())[-6:].zfill(6)
    return f"{digits}.SH" if digits.startswith(("600", "601", "603", "605", "688")) else f"{digits}.SZ"


def _normalize_tushare_close_map(df: pd.DataFrame | None) -> dict[str, float]:
    if df is None or df.empty or not {"trade_date", "close"}.issubset(df.columns):
        return {}
    work = df.copy()
    work["trade_date"] = work["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["trade_date", "close"])
    work = work[work["close"] > 0]
    return {str(day): float(price) for day, price in zip(work["trade_date"], work["close"])}


def _fetch_tushare_close_map(pro, code: str, rows: list[dict[str, Any]], end_date: str) -> dict[str, float] | None:
    rec_dates = [recommend_date_to_yyyymmdd(row.get("recommend_date")) for row in rows]
    rec_dates = [day for day in rec_dates if day]
    if not rec_dates:
        return {}
    ts_code = _to_ts_code(code)
    try:
        return _normalize_tushare_close_map(pro.daily(ts_code=ts_code, start_date=min(rec_dates), end_date=end_date))
    except Exception as exc:
        logger.warning("tushare daily failed %s: %s", ts_code, exc)
        return None


def _build_tushare_tracking_updates(
    pro,
    grouped: dict[str, list[dict[str, Any]]],
    end_date: str,
    now_iso: str,
) -> tuple[list[dict[str, Any]], int, str]:
    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_trade_date = ""
    for code, rows in grouped.items():
        close_map = _fetch_tushare_close_map(pro, code, rows, end_date)
        if not close_map:
            codes_no_data += 1
            continue
        trade_dates = sorted(close_map)
        current_close = float(close_map[trade_dates[-1]])
        latest_trade_date = max(latest_trade_date, trade_dates[-1])
        first_date = first_recommend_date_yyyymmdd(rows)
        for row in rows:
            update = tracking_update_from_close_map(
                row,
                int(code),
                trade_dates,
                close_map,
                current_close,
                now_iso,
                first_recommend_date=first_date,
            )
            if update is not None:
                updates.append(update)
    return updates, codes_no_data, latest_trade_date


def refresh_tracking_prices_with_tickflow_realtime() -> dict[str, Any]:
    if not is_admin_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    require_server_write_context("refresh CN tracking prices")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")
    from integrations.tickflow_client import normalize_cn_symbol

    client = create_admin_client()
    records = fetch_recommendation_tracking_records(client, "id,code,recommend_date")
    if not records:
        return empty_tracking_refresh_summary()
    grouped = _group_records_by_code6(records)
    symbols = [normalize_cn_symbol(code) for code in sorted(grouped)]
    symbols = [symbol for symbol in symbols if symbol]
    batch_size = max(min(int(os.getenv("RECOMMENDATION_TICKFLOW_BATCH_SIZE", "80")), 200), 1)
    quotes, hist_map = fetch_tickflow_tracking_market_data(api_key, symbols, batch_size)
    updates, codes_no_data, latest_trade_date = _build_tickflow_tracking_updates(
        grouped,
        quotes,
        hist_map,
        datetime.now(UTC).isoformat(),
    )
    return _refresh_summary(records, grouped, updates, codes_no_data, latest_trade_date, client)


def refresh_global_tracking_prices(market: str) -> dict[str, Any]:
    if not is_admin_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    require_server_write_context(f"refresh global tracking prices {market}")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")

    client = create_admin_client()
    records = fetch_global_recommendation_tracking_records(client, market, "id,code,recommend_date")
    if not records:
        return empty_tracking_refresh_summary()
    grouped = _group_global_records_by_symbol(records)
    batch_size = max(min(int(os.getenv("RECOMMENDATION_TICKFLOW_BATCH_SIZE", "80")), 200), 1)
    quotes, hist_map = fetch_tickflow_tracking_market_data(api_key, sorted(grouped), batch_size)
    updates, codes_no_data, latest_trade_date = build_global_tickflow_tracking_updates(
        grouped,
        quotes,
        hist_map,
        datetime.now(UTC).isoformat(),
    )
    written = upsert_global_recommendation_tracking_updates(client, market, updates)
    return {
        "rows_total": len(records),
        "rows_updated": written,
        "rows_skipped": max(len(records) - written, 0),
        "codes_total": len(grouped),
        "codes_no_data": codes_no_data,
        "latest_trade_date": latest_trade_date,
    }


def build_global_tickflow_tracking_updates(
    grouped: dict[str, list[dict[str, Any]]],
    quotes: dict[str, dict[str, Any]],
    hist_map: dict[str, pd.DataFrame],
    now_iso: str,
) -> tuple[list[dict[str, Any]], int, str]:
    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_trade_date = ""
    for symbol, rows in grouped.items():
        quote = quotes.get(symbol) or {}
        current_price = resolve_tickflow_quote_price(quote)
        latest_trade_date = max(latest_trade_date, quote_trade_date_yyyymmdd(quote))
        close_map = close_map_from_tickflow_hist(hist_map.get(symbol))
        trade_dates = sorted(close_map)
        current_price = _resolve_global_current_price(current_price, close_map, trade_dates)
        latest_trade_date = max(latest_trade_date, trade_dates[-1] if trade_dates else "")
        if current_price <= 0 or not trade_dates:
            codes_no_data += 1
            continue
        first_date = first_recommend_date_yyyymmdd(rows)
        for row in rows:
            update = tracking_update_from_close_map(
                row,
                symbol,
                trade_dates,
                close_map,
                current_price,
                now_iso,
                first_recommend_date=first_date,
            )
            if update is not None:
                updates.append(update)
    return updates, codes_no_data, latest_trade_date


def _group_global_records_by_symbol(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        symbol = str(row.get("code") or "").strip()
        if symbol:
            grouped.setdefault(symbol, []).append(row)
    return grouped


def _resolve_global_current_price(current_price: float, close_map: dict[str, float], trade_dates: list[str]) -> float:
    if current_price > 0 or not trade_dates:
        return current_price
    return float(close_map[trade_dates[-1]])


def _refresh_summary(
    records: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
    updates: list[dict[str, Any]],
    codes_no_data: int,
    latest_trade_date: str,
    client,
) -> dict[str, Any]:
    written = upsert_recommendation_tracking_updates(client, updates)
    return {
        "rows_total": len(records),
        "rows_updated": written,
        "rows_skipped": max(len(records) - written, 0),
        "codes_total": len(grouped),
        "codes_no_data": codes_no_data,
        "latest_trade_date": latest_trade_date,
    }
