"""Pure recommendation payload builders and backup renderers."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from core.candidate_metadata import CANDIDATE_ATTRIBUTION_COLUMNS, STRATEGY_VERSION_CANDIDATE_LANE_V1
from core.candidate_metadata import code6 as _code6
from core.constants import TABLE_RECOMMENDATION_TRACKING
from utils.safe import safe_float

RECOMMENDATION_ATTRIBUTION_COLUMNS = (
    "primary_signal",
    "signal_types",
    "signal_track",
    "market_regime",
    "selection_source",
    "selection_rank",
    "selection_is_fill",
    "priority_score",
    "trigger_score",
    "stage",
    "industry",
    "sector_state_code",
    "sector_state",
    "sector_note",
    "sector_guidance",
    "exit_signal",
    "exit_price",
    "exit_reason",
    "strategic_theme",
    "strategic_theme_score",
    "strategic_stock_score",
    "strategic_theme_state",
    "strategic_theme_bonus",
    "capital_migration_bonus",
    "springboard_a",
    "springboard_b",
    "springboard_c",
    "springboard_combo",
    "springboard_grade",
    "springboard_met_count",
    "springboard_support",
    "springboard_touch_count",
    "springboard_evidence",
    "springboard_scored",
    *CANDIDATE_ATTRIBUTION_COLUMNS,
)
RECOMMENDATION_OPTIONAL_COLUMNS = (
    "is_ai_recommended",
    "funnel_score",
    "recommend_count",
    *RECOMMENDATION_ATTRIBUTION_COLUMNS,
)
RECOMMENDATION_BACKUP_COLUMNS = (
    "code",
    "name",
    "recommend_reason",
    "recommend_date",
    "initial_price",
    "current_price",
    "change_pct",
    "recommend_count",
    "funnel_score",
    "is_ai_recommended",
    *RECOMMENDATION_ATTRIBUTION_COLUMNS,
    "updated_at",
)
SPRINGBOARD_AI_UPDATE_COLUMNS = (
    "springboard_a",
    "springboard_b",
    "springboard_c",
    "springboard_combo",
    "springboard_grade",
    "springboard_met_count",
    "springboard_support",
    "springboard_touch_count",
    "springboard_evidence",
    "springboard_scored",
)


def build_recommendation_payload(
    recommend_date: int,
    symbols_info: list[dict[str, Any]],
    existing_counts: dict[int, int],
    existing_code_dates: dict[int, set[int]],
) -> list[dict[str, Any]]:
    payload_by_code: dict[int, dict[str, Any]] = {}
    for item in symbols_info:
        code_int = _extract_recommendation_code(item.get("code"))
        if code_int is None:
            continue
        row = _recommendation_row(recommend_date, item, existing_counts, existing_code_dates, code_int)
        existing = payload_by_code.get(code_int)
        if existing:
            merge_recommendation_payload_row(existing, row)
        else:
            payload_by_code[code_int] = row
    return list(payload_by_code.values())


def merge_recommendation_payload_row(existing: dict[str, Any], row: dict[str, Any]) -> None:
    if not existing.get("name") and row.get("name"):
        existing["name"] = row["name"]
    old_score = existing.get("funnel_score")
    new_score = row.get("funnel_score")
    if new_score is not None and (old_score is None or safe_float(new_score) > safe_float(old_score)):
        existing["funnel_score"] = new_score
        existing["recommend_reason"] = row.get("recommend_reason", "")
        _copy_recommendation_attribution(existing, row)
    else:
        _fill_missing_attribution(existing, row)
    old_price = safe_float(existing.get("initial_price"), 0.0)
    new_price = safe_float(row.get("initial_price"), 0.0)
    if old_price <= 0 < new_price:
        existing["initial_price"] = new_price
        existing["current_price"] = new_price


def recommendation_backup_rows(rows: list[dict[str, Any]], ai_codes: list[str] | None) -> list[dict[str, Any]]:
    ai_set = {_code6(code) for code in ai_codes or [] if _code6(code)}
    snapshot = []
    for row in rows:
        clean_row = {col: clean_backup_value(row.get(col)) for col in RECOMMENDATION_BACKUP_COLUMNS if col in row}
        if ai_codes is not None:
            clean_row["is_ai_recommended"] = _code6(clean_row.get("code")) in ai_set
        snapshot.append(clean_row)
    return sorted(snapshot, key=lambda item: int(item.get("code") or 0))


def recommendation_restore_sql(rows: list[dict[str, Any]], table: str = TABLE_RECOMMENDATION_TRACKING) -> str:
    columns = [col for col in RECOMMENDATION_BACKUP_COLUMNS if any(col in row for row in rows)]
    if not rows or not columns:
        return "-- no recommendation rows to restore\n"
    values = ["  (" + ", ".join(_sql_literal(row.get(col)) for col in columns) + ")" for row in rows]
    updates = ",\n  ".join(f"{col} = excluded.{col}" for col in columns if col not in {"code", "recommend_date"})
    return "\n".join(
        [
            "begin;",
            f"insert into public.{table} ({', '.join(columns)})",
            "values",
            ",\n".join(values),
            "on conflict (code, recommend_date) do update set",
            f"  {updates};",
            "commit;",
            "",
        ]
    )


def clean_backup_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, list | tuple | set):
        return [cleaned for item in value if (cleaned := clean_backup_value(item)) is not None]
    if isinstance(value, dict):
        return {str(key): cleaned for key, item in value.items() if (cleaned := clean_backup_value(item)) is not None}
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


def ai_code_ints(ai_codes: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for code in ai_codes or []:
        code_digits = "".join(ch for ch in str(code) if ch.isdigit())
        if not code_digits:
            continue
        code6 = code_digits[-6:].zfill(6)
        try:
            out[code6] = int(code6)
        except Exception:
            continue
    return out


def springboard_ai_payload(fields: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    return {col: clean_backup_value(fields[col]) for col in SPRINGBOARD_AI_UPDATE_COLUMNS if col in fields}


def _recommendation_row(
    recommend_date: int,
    item: dict[str, Any],
    existing_counts: dict[int, int],
    existing_code_dates: dict[int, set[int]],
    code_int: int,
) -> dict[str, Any]:
    old_cnt = existing_counts.get(code_int, 0)
    seen_dates = existing_code_dates.get(code_int, set())
    new_cnt = old_cnt if recommend_date in seen_dates else max(old_cnt, 0) + 1
    price = _extract_recommendation_price(item)
    return {
        "code": code_int,
        "name": str(item.get("name", "")).strip(),
        "recommend_reason": str(item.get("tag", "")).strip(),
        "recommend_date": recommend_date,
        "initial_price": price,
        "current_price": price,
        "change_pct": 0.0,
        "recommend_count": new_cnt,
        "funnel_score": _extract_recommendation_score(item),
        "is_ai_recommended": False,
        "updated_at": datetime.now(UTC).isoformat(),
        **_extract_recommendation_attribution(item),
    }


def _extract_recommendation_code(raw_code: Any) -> int | None:
    code_str = "".join(filter(str.isdigit, str(raw_code or "").strip()))
    return int(code_str) if code_str else None


def _extract_recommendation_price(row: dict[str, Any]) -> float:
    for key in ("initial_price", "current_price", "price", "latest_price", "close"):
        raw_price = row.get(key)
        if raw_price is None or raw_price == "":
            continue
        try:
            parsed = float(raw_price)
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return 0.0


def _extract_recommendation_score(row: dict[str, Any]) -> float | None:
    for score_key in ("funnel_score", "score", "priority_score"):
        raw_score = row.get(score_key)
        if raw_score is None or raw_score == "":
            continue
        if (score := _optional_float(raw_score)) is not None:
            return score
    return None


def _extract_recommendation_attribution(row: dict[str, Any]) -> dict[str, Any]:
    signal_types = _optional_text_list(row.get("signal_types"))
    primary_signal = _optional_text(row.get("primary_signal")) or (signal_types[0] if signal_types else None)
    candidate_lane = (
        _optional_text(row.get("candidate_lane")) or primary_signal or _optional_text(row.get("selection_source"))
    )
    entry_type = _optional_text(row.get("entry_type")) or candidate_lane
    return {
        "primary_signal": primary_signal,
        "signal_types": signal_types,
        "signal_track": _optional_text(row.get("signal_track")) or _optional_text(row.get("track")),
        "market_regime": _optional_text(row.get("market_regime")),
        "selection_source": _optional_text(row.get("selection_source")),
        "selection_rank": _optional_int(row.get("selection_rank") or row.get("priority_rank")),
        "selection_is_fill": _optional_bool(row.get("selection_is_fill")) or False,
        "priority_score": _optional_float(row.get("priority_score")),
        "trigger_score": _optional_float(row.get("trigger_score") if "trigger_score" in row else row.get("score")),
        "stage": _optional_text(row.get("stage")),
        "industry": _optional_text(row.get("industry")),
        "sector_state_code": _optional_text(row.get("sector_state_code")),
        "sector_state": _optional_text(row.get("sector_state")),
        "sector_note": _optional_text(row.get("sector_note")),
        "sector_guidance": _optional_text(row.get("sector_guidance")),
        "exit_signal": _optional_text(row.get("exit_signal")),
        "exit_price": _optional_float(row.get("exit_price")),
        "exit_reason": _optional_text(row.get("exit_reason")),
        "strategic_theme": _optional_text(row.get("strategic_theme")),
        "strategic_theme_score": _optional_float(row.get("strategic_theme_score")),
        "strategic_stock_score": _optional_float(row.get("strategic_stock_score")),
        "strategic_theme_state": _optional_text(row.get("strategic_theme_state")),
        "strategic_theme_bonus": _optional_float(row.get("strategic_theme_bonus")),
        "capital_migration_bonus": _optional_float(row.get("capital_migration_bonus")),
        "springboard_a": _optional_bool(row.get("springboard_a")) or False,
        "springboard_b": _optional_bool(row.get("springboard_b")) or False,
        "springboard_c": _optional_bool(row.get("springboard_c")) or False,
        "springboard_combo": _springboard_combo(row),
        "springboard_grade": _optional_text(row.get("springboard_grade")) or _springboard_combo(row),
        "springboard_met_count": _optional_int(row.get("springboard_met_count")) or 0,
        "springboard_support": _optional_float(row.get("springboard_support")),
        "springboard_touch_count": _optional_int(row.get("springboard_touch_count")) or 0,
        "springboard_evidence": _optional_json(row.get("springboard_evidence")),
        "springboard_scored": _optional_bool(row.get("springboard_scored")) or False,
        "strategy_version": _optional_text(row.get("strategy_version")) or STRATEGY_VERSION_CANDIDATE_LANE_V1,
        "candidate_lane": candidate_lane,
        "entry_type": entry_type,
        "signal_key": _optional_text(row.get("signal_key")) or entry_type,
        "candidate_status": _optional_text(row.get("candidate_status")),
        "candidate_timing": _optional_text(row.get("candidate_timing")),
        "candidate_risk": _optional_text(row.get("candidate_risk")),
        "candidate_reasons": _optional_json(row.get("candidate_reasons")),
        "candidate_metrics": _optional_json(row.get("candidate_metrics")),
        "candidate_theme": _optional_text(row.get("candidate_theme")),
        "candidate_phase": _optional_text(row.get("candidate_phase")),
        "candidate_role": _optional_text(row.get("candidate_role")),
        "mainline_score": _optional_float(row.get("mainline_score")),
        "theme_score": _optional_float(row.get("theme_score")),
        "stock_role_score": _optional_float(row.get("stock_role_score")),
        "quality_score": _optional_float(row.get("quality_score")),
        "timing_score": _optional_float(row.get("timing_score")),
    }


def _copy_recommendation_attribution(target: dict[str, Any], source: dict[str, Any]) -> None:
    for col in RECOMMENDATION_ATTRIBUTION_COLUMNS:
        if not _is_missing_payload_value(source.get(col)) or _is_missing_payload_value(target.get(col)):
            target[col] = source.get(col)


def _fill_missing_attribution(existing: dict[str, Any], row: dict[str, Any]) -> None:
    for col in RECOMMENDATION_ATTRIBUTION_COLUMNS:
        if _is_missing_payload_value(existing.get(col)) and not _is_missing_payload_value(row.get(col)):
            existing[col] = row[col]


def _is_missing_payload_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | set | dict):
        return len(value) == 0
    return False


def _springboard_combo(row: dict[str, Any]) -> str:
    combo = _optional_text(row.get("springboard_combo")) or _optional_text(row.get("springboard_grade"))
    if combo:
        return combo
    parts = [
        name
        for name, key in (("A", "springboard_a"), ("B", "springboard_b"), ("C", "springboard_c"))
        if _optional_bool(row.get(key))
    ]
    return "+".join(parts) if parts else "none"


def _optional_text(raw: Any) -> str | None:
    text = str(raw or "").strip()
    return text or None


def _optional_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except Exception:
        return None
    return value if pd.notna(value) and math.isfinite(value) else None


def _optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _optional_bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if raw is None or raw == "":
        return None
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _optional_text_list(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    values = raw if isinstance(raw, list | tuple | set) else str(raw).split(",")
    return [text for item in values if (text := str(item or "").strip())]


def _optional_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _sql_literal(value: Any) -> str:
    value = clean_backup_value(value)
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "'{}'::text[]"
        return "array[" + ", ".join(_sql_literal(str(item)) for item in value) + "]::text[]"
    if isinstance(value, dict):
        return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"
