"""威科夫库 theme_structure_cross_daily 落库。

只写交叉观察表，按 (trade_date, ts_code) upsert。缺 Radar 凭证时不要调用这里
假装写入空集——那会把「没算」写成「今日无交叉」。
"""

from __future__ import annotations

import logging
from typing import Any

from core.constants import TABLE_THEME_STRUCTURE_CROSS_DAILY
from core.theme_structure_cross_schema import payload_keys
from integrations.supabase_base import create_admin_client, require_server_write_context

logger = logging.getLogger(__name__)
CHUNK = 200
_CONFLICT_KEY = "trade_date,ts_code"


def build_persist_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = payload_keys()
    out: list[dict[str, Any]] = []
    for raw in rows:
        item = {key: raw.get(key) for key in keys}
        if not item.get("trade_date") or not item.get("ts_code"):
            continue
        out.append(item)
    return out


def save_theme_structure_cross_rows(rows: list[dict[str, Any]]) -> int:
    """写入交叉行。空列表表示「算过、今日无交叉」，返回 0 但不报成功句式。"""
    payload = build_persist_rows(rows)
    require_server_write_context(f"{TABLE_THEME_STRUCTURE_CROSS_DAILY} write")
    if not payload:
        logger.info("[theme-cross] %s written=0 (empty day)", TABLE_THEME_STRUCTURE_CROSS_DAILY)
        return 0
    client = create_admin_client()
    written = 0
    failed = 0
    for start in range(0, len(payload), CHUNK):
        batch = payload[start : start + CHUNK]
        try:
            client.table(TABLE_THEME_STRUCTURE_CROSS_DAILY).upsert(batch, on_conflict=_CONFLICT_KEY).execute()
            written += len(batch)
        except Exception as exc:  # noqa: BLE001 - 观察表写失败不中断漏斗
            failed += len(batch)
            logger.warning("[theme-cross] 批次写入失败 rows=%d: %s", len(batch), exc)
    if failed or written < len(payload):
        logger.warning(
            "[theme-cross] %s 未全部写入: written=%d failed=%d total=%d",
            TABLE_THEME_STRUCTURE_CROSS_DAILY,
            written,
            failed,
            len(payload),
        )
    else:
        logger.info("[theme-cross] %s written=%d", TABLE_THEME_STRUCTURE_CROSS_DAILY, written)
    return written


def load_theme_structure_cross_rows(trade_date: str, *, cohort: str = "") -> list[dict[str, Any]]:
    client = create_admin_client()
    query = client.table(TABLE_THEME_STRUCTURE_CROSS_DAILY).select("*").eq("trade_date", trade_date)
    if cohort:
        query = query.eq("cohort", cohort)
    resp = query.order("theme_rank", desc=False).order("ts_code", desc=False).execute()
    return list(resp.data or [])
