"""A-share recommendation payload storage, backup, and AI marking."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.constants import TABLE_RECOMMENDATION_TRACKING
from core.recommendation_payload import (
    RECOMMENDATION_OPTIONAL_COLUMNS,
    ai_code_ints,
    build_recommendation_payload,
    recommendation_backup_rows,
    recommendation_restore_sql,
    springboard_ai_payload,
)
from integrations.recommendation_tracking_common import (
    chunked as _chunked,
)
from integrations.recommendation_tracking_common import (
    fetch_records_from_table,
)
from integrations.supabase_base import create_admin_client as _get_supabase_admin_client
from integrations.supabase_base import is_admin_configured as is_supabase_configured
from integrations.supabase_base import require_server_write_context

logger = logging.getLogger(__name__)


def _load_existing_recommendation_history(
    client,
) -> tuple[dict[int, int], dict[int, set[int]], dict[int, float]]:
    existing_counts: dict[int, int] = {}
    existing_code_dates: dict[int, set[int]] = {}
    first_dates: dict[int, int] = {}
    first_prices: dict[int, float] = {}
    all_rows = fetch_records_from_table(
        client, TABLE_RECOMMENDATION_TRACKING, "code,recommend_count,recommend_date,initial_price"
    )
    for row in all_rows:
        try:
            code_int = int(row.get("code"))
        except (TypeError, ValueError):
            continue
        cnt = int(row.get("recommend_count") or 1) if row.get("recommend_count") else 1
        existing_counts[code_int] = max(existing_counts.get(code_int, 0), cnt)
        try:
            d = int(row.get("recommend_date"))
        except (TypeError, ValueError):
            logger.debug("invalid recommend_date for code %s", row.get("code"), exc_info=True)
            continue
        existing_code_dates.setdefault(code_int, set()).add(d)
        _remember_first_recommend_price(first_dates, first_prices, code_int, d, row.get("initial_price"))
    return existing_counts, existing_code_dates, first_prices


def _remember_first_recommend_price(
    first_dates: dict[int, int],
    first_prices: dict[int, float],
    code_int: int,
    recommend_date: int,
    raw_price: Any,
) -> None:
    try:
        price = float(raw_price or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    prev_date = first_dates.get(code_int)
    if prev_date is None or recommend_date < prev_date:
        first_dates[code_int] = recommend_date
        first_prices[code_int] = price if price > 0 else 0.0
        return
    if recommend_date == prev_date and price > 0:
        first_prices[code_int] = price


def upsert_recommendation_payload_rows(client, payload: list[dict[str, Any]]) -> None:
    if not payload:
        return
    compatible_payload = payload
    dropped_columns: set[str] = set()
    while True:
        try:
            for chunk in _chunked(compatible_payload, 500):
                client.table(TABLE_RECOMMENDATION_TRACKING).upsert(chunk, on_conflict="code,recommend_date").execute()
            return
        except Exception as exc:
            missing = _missing_optional_columns(exc) - dropped_columns
            if not missing:
                raise
            dropped_columns.update(missing)
            logger.warning("recommendation_tracking missing optional columns; retrying without %s", sorted(missing))
            compatible_payload = [
                {key: value for key, value in row.items() if key not in missing} for row in compatible_payload
            ]


def _missing_optional_columns(exc: Exception) -> set[str]:
    message = str(exc).lower()
    return {column for column in RECOMMENDATION_OPTIONAL_COLUMNS if column.lower() in message}


def prepare_recommendation_payload(recommend_date: int, symbols_info: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not is_supabase_configured() or not symbols_info:
        return []
    client = _get_supabase_admin_client()
    existing_counts, existing_code_dates, first_prices = _load_existing_recommendation_history(client)
    return build_recommendation_payload(
        recommend_date,
        symbols_info,
        existing_counts,
        existing_code_dates,
        first_prices,
    )


def upsert_recommendation_payload(payload: list[dict[str, Any]]) -> bool:
    if not is_supabase_configured() or not payload:
        return False
    require_server_write_context("upsert recommendation_tracking")
    try:
        client = _get_supabase_admin_client()
        upsert_recommendation_payload_rows(client, payload)
        return True
    except Exception as e:
        logger.warning("upsert_recommendation_payload failed: %s", e)
        return False


def upsert_recommendations(recommend_date: int, symbols_info: list[dict[str, Any]]) -> bool:
    """
    将每日选出的股票存入形态复盘表
    recommend_date: YYYYMMDD (int)
    """
    if not is_supabase_configured() or not symbols_info:
        return False
    try:
        payload = prepare_recommendation_payload(recommend_date, symbols_info)

        # 使用 upsert，基于 (code, recommend_date) 唯一约束：
        # - 同一只股票在同一天重跑会覆盖更新（initial_price 粘住首次推荐日收盘）；
        # - 跨天会新增一条记录，推荐价仍用该 code 首次推荐日收盘；
        # - recommend_count 按 code 维度累计。
        return upsert_recommendation_payload(payload)
    except Exception as e:
        logger.warning("upsert_recommendations failed: %s", e)
        return False


def write_recommendation_backup_artifact(
    recommend_date: int,
    rows: list[dict[str, Any]],
    output_dir: str,
    *,
    ai_codes: list[str] | None = None,
) -> list[str]:
    if not output_dir or not rows:
        return []
    snapshot = recommendation_backup_rows(rows, ai_codes)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    base = f"recommendation_tracking_{recommend_date}"
    json_path = target / f"{base}.json"
    sql_path = target / f"{base}.sql"
    payload = {
        "table": f"public.{TABLE_RECOMMENDATION_TRACKING}",
        "recommend_date": recommend_date,
        "row_count": len(snapshot),
        "generated_at": datetime.now(UTC).isoformat(),
        "rows": snapshot,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sql_path.write_text(recommendation_restore_sql(snapshot), encoding="utf-8")
    return [str(json_path), str(sql_path)]


def mark_ai_recommendations(
    recommend_date: int,
    ai_codes: list[str],
    springboard_updates: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """
    将某个推荐日的记录标记为是否 AI 推荐（可操作池）。
    ai_codes 传入 6 位代码字符串列表。
    """
    if not is_supabase_configured():
        return False
    require_server_write_context("mark AI recommendations")
    try:
        client = _get_supabase_admin_client()
        now_iso = datetime.now(UTC).isoformat()
        # 先全量置 false，再对白名单置 true，避免前一次残留。
        client.table(TABLE_RECOMMENDATION_TRACKING).update({"is_ai_recommended": False, "updated_at": now_iso}).eq(
            "recommend_date", recommend_date
        ).execute()

        code_map = ai_code_ints(ai_codes)
        if code_map:
            code_ints = sorted(set(code_map.values()))
            client.table(TABLE_RECOMMENDATION_TRACKING).update({"is_ai_recommended": True, "updated_at": now_iso}).eq(
                "recommend_date", recommend_date
            ).in_("code", code_ints).execute()
        updates = springboard_updates or {}
        for code6, code_int in code_map.items():
            payload = springboard_ai_payload(updates.get(code6))
            if not payload:
                continue
            payload.update({"is_ai_recommended": True, "updated_at": now_iso})
            client.table(TABLE_RECOMMENDATION_TRACKING).update(payload).eq("recommend_date", recommend_date).eq(
                "code", code_int
            ).execute()
        return True
    except Exception as e:
        msg = str(e)
        if "is_ai_recommended" in msg:
            logger.warning("mark_ai_recommendations skipped: missing column is_ai_recommended")
            return False
        logger.warning("mark_ai_recommendations failed: %s", e)
        return False
