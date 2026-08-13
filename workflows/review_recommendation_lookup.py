"""Recommendation tracking lookup for review-list replay reports."""

from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pandas as pd


def load_recommendation_lookup(codes: list[str]) -> tuple[dict[str, list[dict]], str]:
    clean_codes = sorted({int(code) for code in (normalize_code6(raw) for raw in codes) if code})
    if not clean_codes:
        return {}, ""
    try:
        return _load_recommendation_rows(clean_codes), ""
    except Exception as exc:
        print(f"[review] 推荐表读取失败: {exc}")
        return {}, "推荐表读取失败，无法确认是否被推荐过"


def recommendation_state(records: list[dict], target_date: str) -> dict[str, object]:
    matched = [row for row in records if normalize_recommend_date(row.get("recommend_date")) == target_date]
    if not matched:
        return {"tracked": False, "ai_recommended": False, "statuses": [], "sources": []}
    return {
        "tracked": True,
        "ai_recommended": any(bool(row.get("is_ai_recommended")) for row in matched),
        "statuses": _unique_text(matched, "candidate_status"),
        "sources": _unique_text(matched, "selection_source"),
    }


def format_recommendation_history(
    code: str,
    lookup: dict[str, list[dict]],
    load_error: str = "",
    exclude_date: date | None = None,
) -> str:
    if load_error:
        return f"推荐记录: {load_error}"
    records = lookup.get(normalize_code6(code), [])
    records = _exclude_recommend_date(records, exclude_date)
    if not records:
        return "推荐记录: 此股没被推荐过"

    dates = sorted({normalize_recommend_date(row.get("recommend_date")) for row in records}, reverse=True)
    count = _recommendation_count(records, dates)
    return f"推荐记录: {'、'.join(dates)} 被推荐过；累计推荐{count}次"


def normalize_code6(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def normalize_recommend_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "日期未知"
    try:
        parsed = pd.to_datetime(text, format="%Y%m%d") if len(text) == 8 and text.isdigit() else pd.to_datetime(text)
        if pd.isna(parsed):
            return text
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return text


def _load_recommendation_rows(clean_codes: list[int]) -> dict[str, list[dict]]:
    from core.constants import TABLE_RECOMMENDATION_TRACKING
    from integrations.supabase_base import create_admin_client, is_admin_configured

    if not is_admin_configured():
        return {}
    client = create_admin_client()
    rows: list[dict] = []
    for chunk in _chunks(clean_codes, 200):
        response = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select("code,name,recommend_date,recommend_count,is_ai_recommended,candidate_status,selection_source")
            .in_("code", chunk)
            .order("recommend_date", desc=True)
            .limit(10000)
            .execute()
        )
        rows.extend([row for row in (response.data or []) if isinstance(row, dict)])
    return _rows_by_code(rows)


def _rows_by_code(rows: list[dict]) -> dict[str, list[dict]]:
    lookup: dict[str, list[dict]] = {}
    for row in rows:
        code = normalize_code6(row.get("code"))
        if code:
            lookup.setdefault(code, []).append(row)
    return lookup


def _exclude_recommend_date(records: list[dict], exclude_date: date | None) -> list[dict]:
    if not exclude_date:
        return records
    exclude_text = exclude_date.strftime("%Y-%m-%d")
    return [row for row in records if normalize_recommend_date(row.get("recommend_date")) != exclude_text]


def _recommendation_count(records: list[dict], dates: list[str]) -> int:
    parsed_counts: list[int] = []
    for row in records:
        with contextlib.suppress(Exception):
            parsed_counts.append(int(row.get("recommend_count") or 0))
    return max([len(dates), *parsed_counts]) if parsed_counts else len(dates)


def _chunks(items: list[int], size: int) -> list[list[int]]:
    width = max(int(size), 1)
    return [items[index : index + width] for index in range(0, len(items), width)]


def _unique_text(rows: list[dict], key: str) -> list[str]:
    return list(dict.fromkeys(str(row.get(key) or "").strip() for row in rows if str(row.get(key) or "").strip()))
