"""Supabase recommendation_tracking table adapter."""

from __future__ import annotations

import logging
from typing import Any

from core.constants import TABLE_RECOMMENDATION_TRACKING
from integrations.recommendation_tracking_common import chunked
from integrations.supabase_base import create_read_client

logger = logging.getLogger(__name__)

# 市场 -> 表名。三个市场是三张独立的表，不是同一张表里的一个字段，
# 所以「切市场」等于换表重查，不能在客户端过滤。
#
# 用白名单映射而不是拼字符串：market 来自前端，拼进表名等于把表名交给调用方。
RECOMMENDATION_TABLES = {
    "cn": TABLE_RECOMMENDATION_TRACKING,
    "us": f"{TABLE_RECOMMENDATION_TRACKING}_us",
    "hk": f"{TABLE_RECOMMENDATION_TRACKING}_hk",
}


def recommendation_table(market: str = "cn") -> str:
    """认不出的市场退回 cn —— 宁可显示 A 股，也不要去查一张不存在的表。"""
    return RECOMMENDATION_TABLES.get(str(market or "cn").strip().lower(), TABLE_RECOMMENDATION_TRACKING)


def fetch_recommendation_tracking_records(
    client,
    select_expr: str = "*",
    *,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = max(min(int(page_size), 1000), 1)
    start = 0
    while True:
        resp = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
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


def upsert_recommendation_tracking_updates(client, updates: list[dict[str, Any]], batch_size: int = 500) -> int:
    written = 0
    rows = [row for row in updates if row.get("code") is not None and row.get("recommend_date") is not None]
    for batch in chunked(rows, max(min(int(batch_size), 1000), 1)):
        client.table(TABLE_RECOMMENDATION_TRACKING).upsert(batch, on_conflict="code,recommend_date").execute()
        written += len(batch)
    return written


def upsert_recommendation_tracking_price_updates(client, updates: list[dict[str, Any]], batch_size: int = 50) -> int:
    written = 0
    rows = [row for row in updates if row.get("id") is not None]
    for batch in chunked(rows, max(int(batch_size), 1)):
        client.table(TABLE_RECOMMENDATION_TRACKING).upsert(batch, on_conflict="id").execute()
        written += len(batch)
    return written


def load_recommendation_tracking(limit: int = 1000, client=None, market: str = "cn") -> list[dict[str, Any]]:
    """
    读某个市场的推荐跟踪记录。

    美股/港股表只有 34 列（CN 有 76），没有 candidate_* 那一组。缺列不是错误，
    调用方按缺省处理即可 —— 不要为了对齐而伪造字段。
    """
    table = recommendation_table(market)
    try:
        db = client or create_read_client()
        resp = db.table(table).select("*").order("recommend_date", desc=True).limit(limit).execute()
        return resp.data or []
    except Exception as exc:
        logger.warning("load_recommendation_tracking(%s) failed: %s", table, exc)
        return []
