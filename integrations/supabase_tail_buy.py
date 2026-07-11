"""Supabase tail_buy_history 表读写。"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from core.candidate_metadata import code6 as _code6
from core.constants import TABLE_TAIL_BUY_HISTORY
from integrations.supabase_base import create_admin_client as _admin
from integrations.supabase_base import create_read_client as _read
from integrations.supabase_base import is_admin_configured as _configured
from integrations.supabase_base import require_server_write_context
from utils.safe import safe_float as _safe_float

logger = logging.getLogger(__name__)

_LEGACY_COLUMNS = {
    "code",
    "name",
    "run_date",
    "signal_date",
    "signal_type",
    "final_decision",
    "rule_score",
    "priority_score",
    "rule_reasons",
    "llm_decision",
    "llm_reason",
    "user_id",
}


def _get_user_id() -> str:
    return os.getenv("SUPABASE_USER_ID", "").strip()


def _payload_row(row: dict, user_id: str) -> dict[str, Any]:
    payload = {
        "code": row["code"],
        "name": row.get("name", ""),
        "run_date": row["run_date"],
        "signal_date": row.get("signal_date", ""),
        "signal_type": row.get("signal_type", ""),
        "status": row.get("status", ""),
        "candidate_theme": row.get("candidate_theme", ""),
        "candidate_phase": row.get("candidate_phase", ""),
        "candidate_role": row.get("candidate_role", ""),
        "mainline_score": row.get("mainline_score"),
        "theme_score": row.get("theme_score"),
        "stock_role_score": row.get("stock_role_score"),
        "final_decision": row.get("final_decision", "BUY"),
        "rule_decision": row.get("rule_decision", ""),
        "rule_score": float(row.get("rule_score", 0)),
        "priority_score": float(row.get("priority_score", 0)),
        "rule_reasons": row.get("rule_reasons", ""),
        "llm_decision": row.get("llm_decision", ""),
        "llm_reason": row.get("llm_reason", ""),
        "llm_confidence": row.get("llm_confidence"),
        "llm_model_used": row.get("llm_model_used", ""),
        "initial_price": float(row.get("initial_price", 0) or 0),
        "current_price": float(row.get("current_price", 0) or 0),
        "change_pct": float(row.get("change_pct", 0) or 0),
        "price_updated_at": str(row.get("price_updated_at") or "").strip() or None,
        "last_close": float(row.get("last_close", 0) or 0),
        "vwap": float(row.get("vwap", 0) or 0),
        "dist_vwap_pct": float(row.get("dist_vwap_pct", 0) or 0),
        "close_pos": float(row.get("close_pos", 0) or 0),
        "day_ret_pct": float(row.get("day_ret_pct", 0) or 0),
        "last30_ret_pct": float(row.get("last30_ret_pct", 0) or 0),
        "last15_ret_pct": float(row.get("last15_ret_pct", 0) or 0),
        "tail30_volume_share": float(row.get("tail30_volume_share", 0) or 0),
        "drop_from_high_pct": float(row.get("drop_from_high_pct", 0) or 0),
        "fetch_error": row.get("fetch_error", ""),
        "features_json": _json_value(row.get("features_json")),
        "user_id": user_id,
    }
    return payload


def _json_value(raw: Any) -> Any:
    if isinstance(raw, dict | list):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _legacy_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _LEGACY_COLUMNS if key in row}


def _looks_like_schema_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return "column" in text or "schema cache" in text or "could not find" in text


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    step = max(int(size), 1)
    return [items[i : i + step] for i in range(0, len(items), step)]


def _resolve_quote_price(quote: dict[str, Any] | None) -> float:
    row = quote or {}
    for key in ("last_price", "close", "last", "price", "current"):
        value = _safe_float(row.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def _fetch_tail_quotes(api_key: str, symbols: list[str], batch_size: int) -> dict[str, dict[str, Any]]:
    from integrations.tickflow_client import TickFlowClient

    tf_client = TickFlowClient(api_key=api_key)
    quotes: dict[str, dict[str, Any]] = {}
    for chunk in _chunked(symbols, batch_size):
        quotes.update(tf_client.get_quotes(chunk))
    return quotes


def _build_tail_price_update(row: dict[str, Any], current_price: float, now_iso: str) -> dict[str, Any]:
    initial_price = _safe_float(row.get("initial_price"), 0.0)
    if initial_price <= 0:
        initial_price = _safe_float(row.get("last_close"), 0.0)
    if initial_price <= 0:
        initial_price = current_price
    change_pct = (current_price - initial_price) / initial_price * 100.0 if initial_price > 0 else 0.0
    return {
        "initial_price": round(initial_price, 4),
        "current_price": round(current_price, 4),
        "change_pct": round(change_pct, 2),
        "price_updated_at": now_iso,
    }


def _fetch_tail_price_records(client, limit: int, user_id: str) -> list[dict[str, Any]]:
    query = client.table(TABLE_TAIL_BUY_HISTORY).select("code,run_date,user_id,initial_price,current_price,last_close")
    if user_id:
        query = query.eq("user_id", user_id)
    resp = query.order("run_date", desc=True).limit(max(min(int(limit), 5000), 1)).execute()
    return resp.data or []


def _execute_tail_price_update(client, row: dict[str, Any], update: dict[str, Any]) -> bool:
    user_id = str(row.get("user_id") or "").strip()
    if not user_id:
        return False
    query = (
        client.table(TABLE_TAIL_BUY_HISTORY)
        .update(update)
        .eq("code", row.get("code"))
        .eq(
            "run_date",
            row.get("run_date"),
        )
    )
    query = query.eq("user_id", user_id)
    query.execute()
    return True


def save_tail_buy_to_supabase(rows: list[dict], user_id: str = "") -> int:
    """写入 BUY 记录到 Supabase，upsert on (code, run_date, user_id)。"""
    if not _configured() or not rows:
        return 0
    require_server_write_context("upsert tail_buy_history")
    user_id = user_id.strip() or _get_user_id()
    if not user_id:
        logger.info("tail_buy write skipped: user_id not provided")
        return 0
    payload = [_payload_row(r, user_id) for r in rows]
    client = None
    try:
        client = _admin()
        client.table(TABLE_TAIL_BUY_HISTORY).upsert(payload, on_conflict="code,run_date,user_id").execute()
        return len(payload)
    except Exception as e:
        if client is not None and _looks_like_schema_miss(e):
            try:
                legacy_payload = [_legacy_row(r) for r in payload]
                client.table(TABLE_TAIL_BUY_HISTORY).upsert(
                    legacy_payload,
                    on_conflict="code,run_date,user_id",
                ).execute()
                logger.warning("tail_buy extended columns missing; wrote legacy payload")
                return len(legacy_payload)
            except Exception as legacy_exc:
                logger.warning("tail_buy legacy supabase write failed: %s", legacy_exc)
                return 0
        logger.warning("tail_buy supabase write failed: %s", e)
        return 0


def _tail_price_refresh_api_key() -> str:
    if not _configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    require_server_write_context("refresh tail_buy_history prices")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")
    return api_key


def refresh_tail_buy_prices_with_tickflow_realtime(limit: int = 1000, user_id: str = "") -> dict[str, Any]:
    """刷新尾盘记录 current_price/change_pct，initial_price 保持写入时价格。"""
    api_key = _tail_price_refresh_api_key()

    from integrations.tickflow_client import normalize_cn_symbol

    client = _admin()
    records = _fetch_tail_price_records(client, limit, user_id.strip())
    grouped = {code6: normalize_cn_symbol(code6) for code6 in {_code6(r.get("code")) for r in records} if code6}
    symbols = [sym for sym in grouped.values() if sym]
    if not records or not symbols:
        return {"rows_total": len(records), "rows_updated": 0, "rows_skipped": len(records), "codes_total": 0}

    batch_size = max(min(int(os.getenv("TAIL_BUY_TICKFLOW_BATCH_SIZE", "120")), 300), 1)
    quotes = _fetch_tail_quotes(api_key, sorted(symbols), batch_size)
    now_iso = datetime.now(UTC).isoformat()
    updated = 0
    no_price = 0
    try:
        for row in records:
            sym = grouped.get(_code6(row.get("code")), "")
            current_price = _resolve_quote_price(quotes.get(sym))
            if current_price <= 0:
                no_price += 1
                continue
            if _execute_tail_price_update(client, row, _build_tail_price_update(row, current_price, now_iso)):
                updated += 1
    except Exception as exc:
        if _looks_like_schema_miss(exc):
            return {
                "rows_total": len(records),
                "rows_updated": updated,
                "rows_skipped": len(records) - updated,
                "codes_total": len(grouped),
                "codes_no_data": no_price,
                "schema_missing": True,
            }
        raise
    return {
        "rows_total": len(records),
        "rows_updated": updated,
        "rows_skipped": len(records) - updated,
        "codes_total": len(grouped),
        "codes_no_data": no_price,
        "schema_missing": False,
    }


def load_tail_buy_from_supabase(limit: int = 100, user_id: str = "", client=None) -> list[dict[str, Any]]:
    """读取最近 N 条尾盘买入记录。"""
    user_id = user_id.strip() or _get_user_id()
    if not user_id:
        logger.info("tail_buy read skipped: user_id not provided")
        return []
    try:
        client = client or _read()
        q = client.table(TABLE_TAIL_BUY_HISTORY).select("*").eq("user_id", user_id)
        resp = q.order("run_date", desc=True).limit(limit).execute()
        return resp.data or []
    except Exception as e:
        logger.warning("tail_buy supabase read failed: %s", e)
        return []
