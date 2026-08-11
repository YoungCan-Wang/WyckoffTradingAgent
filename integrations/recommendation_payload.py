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


def _write_step3_verdicts(
    client: Any,
    recommend_date: int,
    step3_verdicts: dict[str, str] | None,
    now_iso: str,
) -> None:
    """按判定分组批量写 step3_verdict，失败不影响 AI 标记主流程。

    该字段是纯观测数据，不参与任何交易决策，因此写失败只告警不抛出——不能因为
    一个诊断字段拖垮每日入库。但列缺失会被明确记为 ERROR 并给出 DDL，否则功能
    会静默失效数月而无人察觉（本次上线前该列确实不存在）。

    所需 DDL（Supabase schema 不在本仓库管理，需手动执行一次）：
        ALTER TABLE recommendation_tracking ADD COLUMN IF NOT EXISTS step3_verdict text;
    """
    if not step3_verdicts:
        return
    by_verdict: dict[str, list[int]] = {}
    for code6, verdict in step3_verdicts.items():
        code_int = _safe_code_int(code6)
        if code_int is None or not str(verdict or "").strip():
            continue
        by_verdict.setdefault(str(verdict).strip(), []).append(code_int)
    for verdict, code_ints in by_verdict.items():
        try:
            client.table(TABLE_RECOMMENDATION_TRACKING).update({"step3_verdict": verdict, "updated_at": now_iso}).eq(
                "recommend_date", recommend_date
            ).in_("code", sorted(set(code_ints))).execute()
        except Exception as exc:
            if "step3_verdict" in str(exc) and "does not exist" in str(exc):
                logger.error(
                    "step3_verdict 列不存在，LLM 判定未落库。请执行一次: "
                    "ALTER TABLE recommendation_tracking ADD COLUMN IF NOT EXISTS step3_verdict text;"
                )
            else:
                logger.warning("step3_verdict 写入失败 verdict=%s count=%d", verdict, len(code_ints), exc_info=True)
            return


def _safe_code_int(code6: object) -> int | None:
    try:
        text = str(code6 or "").strip()
        return int(text) if text.isdigit() else None
    except (TypeError, ValueError):
        return None


def mark_ai_recommendations(
    recommend_date: int,
    ai_codes: list[str],
    springboard_updates: dict[str, dict[str, Any]] | None = None,
    step3_verdicts: dict[str, str] | None = None,
) -> bool:
    """
    将某个推荐日的记录标记为是否 AI 推荐（可操作池）。
    ai_codes 传入 6 位代码字符串列表。

    step3_verdicts 是 code6 -> invalidated/building/springboard 的完整三分类。
    只标记放行码（is_ai_recommended）无法评估 LLM——被否决的候选不留痕迹，
    事后就无法回答"拦对了吗"。把判定一并写入后，跟踪表里每个候选都带 LLM 结论，
    配合既有的 change_pct/MFE/MAE 即可算出 LLM 的真实区分度。
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
        _write_step3_verdicts(client, recommend_date, step3_verdicts, now_iso)

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
