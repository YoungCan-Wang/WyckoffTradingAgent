"""Supabase concept_heat_history 表读写。"""

from __future__ import annotations

import logging
from typing import Any

from core.concept_filters import is_actionable_theme_name
from core.constants import TABLE_CONCEPT_HEAT_HISTORY
from integrations.supabase_base import close_client as _close
from integrations.supabase_base import create_admin_client as _admin
from integrations.supabase_base import create_read_client as _read
from integrations.supabase_base import is_admin_configured as _configured
from integrations.supabase_base import require_server_write_context

logger = logging.getLogger(__name__)


def _top_heat_items(heat: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    clean = [item for item in heat if is_actionable_theme_name(str(item.get("name", "")))]
    limit = max(int(top_n), 1)
    selected: dict[str, dict[str, Any]] = {}
    for item in sorted(clean, key=lambda x: x.get("net_inflow", 0), reverse=True)[:limit]:
        selected[str(item.get("name"))] = item
    for item in sorted(clean, key=lambda x: x.get("pct", 0), reverse=True)[:limit]:
        selected[str(item.get("name"))] = item
    return list(selected.values())


def upsert_concept_heat_history(trade_date: str, heat: list[dict[str, Any]], top_n: int = 20) -> int:
    """写入概念热度历史，upsert on (trade_date, concept_name)。"""
    if not _configured() or not trade_date or not heat:
        return 0
    require_server_write_context("upsert concept_heat_history")
    payload = []
    for rank, item in enumerate(_top_heat_items(heat, top_n), 1):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        payload.append(
            {
                "trade_date": trade_date,
                "concept_name": name,
                "pct": float(item.get("pct", 0.0) or 0.0),
                "net_inflow": float(item.get("net_inflow", item.get("inflow", 0.0)) or 0.0),
                "rank": rank,
                "source_id": str(item.get("cid", "") or ""),
            }
        )
    if not payload:
        return 0
    client = None
    try:
        client = _admin()
        client.table(TABLE_CONCEPT_HEAT_HISTORY).delete().eq("trade_date", trade_date).execute()
        client.table(TABLE_CONCEPT_HEAT_HISTORY).upsert(
            payload,
            on_conflict="trade_date,concept_name",
        ).execute()
        return len(payload)
    except Exception as exc:
        logger.warning("concept heat write failed: %s", exc)
        return 0
    finally:
        if client is not None:
            _close(client)


def _fetch_concept_heat_rows(client: Any, row_limit: int, as_of_date: str | None) -> list[dict[str, Any]]:
    query = client.table(TABLE_CONCEPT_HEAT_HISTORY).select("trade_date,concept_name,pct,net_inflow,rank")
    if as_of_date:
        query = query.lte("trade_date", as_of_date)
    resp = query.order("trade_date", desc=True).order("rank").limit(row_limit).execute()
    return list(resp.data or [])


def load_concept_heat_history_from_supabase(limit_days: int = 20, as_of_date: str | None = None) -> dict[str, dict]:
    """读取最近 N 个交易日的概念热度历史。

    anon 角色在 concept_heat_history 上被 RLS 挡住,返回的是 count=0 而不是报错——
    与「表为空」完全同形。回放靠这张表还原当日板块热度,读空不会抛异常,只会静默
    退化成 concept_heat=[](2026-09-07 实测:回放拿到 0 个概念,库里同日有 31 个),
    L3 板块过滤和主线打分因此偏离生产。故读空且 service key 可用时升级重读一次。
    """
    row_limit = max(int(limit_days), 1) * 50
    rows: list[dict[str, Any]] = []
    client = None
    try:
        client = _read()
        rows = _fetch_concept_heat_rows(client, row_limit, as_of_date)
    except Exception as exc:
        logger.warning("concept heat read failed: %s", exc)
        rows = []
    finally:
        if client is not None:
            _close(client)

    if not rows and _configured():
        admin = None
        try:
            admin = _admin()
            rows = _fetch_concept_heat_rows(admin, row_limit, as_of_date)
            if rows:
                logger.warning("concept heat: anon 读到 0 行,已用 service key 重读到 %d 行", len(rows))
        except Exception as exc:
            logger.warning("concept heat admin re-read failed: %s", exc)
        finally:
            if admin is not None:
                _close(admin)

    return _rows_to_history(rows, limit_days)


def _rows_to_history(rows: list[dict[str, Any]], limit_days: int) -> dict[str, dict]:
    history: dict[str, dict] = {}
    for row in rows:
        day = str(row.get("trade_date", "")).strip()
        name = str(row.get("concept_name", "")).strip()
        if not day or not name:
            continue
        history.setdefault(day, {})[name] = {
            "pct": float(row.get("pct", 0.0) or 0.0),
            "inflow": float(row.get("net_inflow", 0.0) or 0.0),
        }
    sorted_dates = sorted(history.keys(), reverse=True)[: max(int(limit_days), 1)]
    return {day: history[day] for day in sorted_dates}
