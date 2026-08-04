"""Safe recommendation_tracking backfill workflow."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import workflows.daily_signal_observations as signal_observations
from core.constants import TABLE_RECOMMENDATION_TRACKING
from core.market_trade_mode import resolve_market_trade_mode
from core.recommendation_payload import (
    ai_code_ints,
    build_recommendation_payload,
    recommendation_backup_rows,
    springboard_ai_payload,
)
from core.signal_confirmation import run_confirmation_cycle
from integrations.recommendation_payload import upsert_recommendation_payload_rows
from integrations.recommendation_performance import refresh_tracking_performance
from integrations.recommendation_tracking_common import chunked, fetch_records_from_table
from integrations.supabase_base import (
    CLI_WRITE_CONTEXT,
    WRITE_CONTEXT_ENV,
    create_admin_client,
    require_server_write_context,
)
from integrations.supabase_signal_feedback import upsert_signal_observations
from integrations.supabase_theme_radar import build_theme_radar_snapshot_row
from workflows.daily_job_persistence import (
    benchmark_context_payload,
    recommendation_write_symbols,
    step3_review_symbols,
)
from workflows.daily_job_step2 import (
    run_signal_confirmation,
    run_springboard_scoring,
)
from workflows.daily_job_step3 import parse_step3_springboards


@dataclass(frozen=True)
class RecommendationBackfillRequest:
    dates: tuple[date, ...]
    output_dir: str
    apply: bool = False
    skip_step3: bool = False
    allow_empty_date: bool = False


def run_recommendation_backfill(request: RecommendationBackfillRequest) -> int:
    target_dates = tuple(sorted(set(request.dates)))
    if not target_dates:
        raise ValueError("dates 不能为空")
    output_dir = Path(request.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if request.apply:
        require_server_write_context("backfill recommendation_tracking")

    print(f"[recommendation-backfill] target_dates={','.join(d.isoformat() for d in target_dates)}")
    day_results = [_build_day_result(day, request.skip_step3) for day in target_dates]
    payloads = _build_payloads(target_dates, day_results)
    table_rows = _build_table_rows(day_results)
    client = create_admin_client()
    old_rows = _fetch_target_rows(client, target_dates)
    _write_artifacts(output_dir, target_dates, day_results, payloads, old_rows, table_rows)
    _validate_payloads(payloads, allow_empty_date=request.allow_empty_date)
    if not request.apply:
        print("[recommendation-backfill] dry-run 完成，未写库。确认 artifact 后加 --apply 执行替换。")
        return 0

    summary = _replace_target_dates(client, payloads, old_rows)
    table_summary = _replace_auxiliary_tables(client, target_dates, table_rows)
    summary.update(table_summary)
    _refresh_performance()
    _write_json(output_dir / "apply_summary.json", summary)
    print(
        "[recommendation-backfill] apply 完成: "
        f"upserted={summary['rows_upserted']}, stale_deleted={summary['stale_deleted']}, "
        f"signal_pending={summary['signal_pending_inserted']}, "
        f"signal_observations={summary['signal_observations_upserted']}"
    )
    return 0


def _build_day_result(trade_day: date, skip_step3: bool) -> dict[str, Any]:
    with _day_env(trade_day, skip_step3):
        from workflows.wyckoff_funnel import run as run_funnel

        ok, symbols_info, benchmark_context, details = run_funnel(
            "",
            notify=False,
            return_details=True,
            include_financial_metrics=False,
        )
        if not ok:
            raise RuntimeError(f"{trade_day.isoformat()} funnel run failed")
        trade_mode = _prepare_symbols_for_recommendation(symbols_info, details, benchmark_context)
        write_symbols = recommendation_write_symbols(
            symbols_info,
            step2_details=details,
            benchmark_context=benchmark_context,
            trade_mode=trade_mode,
        )
        review_symbols = step3_review_symbols(symbols_info) if trade_mode.allow_ai_review else []
        ai_codes, springboard_updates = _run_step3_if_needed(
            trade_day,
            review_symbols,
            benchmark_context,
            skip_step3,
        )
        return {
            "trade_date": trade_day.isoformat(),
            "recommend_date": int(trade_day.strftime("%Y%m%d")),
            "raw_count": len(symbols_info),
            "write_count": len(write_symbols),
            "ai_codes": ai_codes,
            "springboard_updates": springboard_updates,
            "benchmark_context": benchmark_context,
            "symbols_info": write_symbols,
            "step2_details": details,
        }


def _prepare_symbols_for_recommendation(
    symbols_info: list[dict],
    details: dict,
    benchmark_context: dict,
) -> Any:
    run_signal_confirmation(symbols_info, details, benchmark_context, None, dry_run=True)
    run_springboard_scoring(symbols_info, details)
    trade_mode = resolve_market_trade_mode((benchmark_context or {}).get("regime"))
    return trade_mode


def _run_step3_if_needed(
    trade_day: date,
    write_symbols: list[dict],
    benchmark_context: dict,
    skip_step3: bool,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if skip_step3 or not write_symbols:
        return [], {}
    from integrations.llm_client import get_provider_credentials, resolve_provider_name
    from workflows.step3_batch_report import run as run_step3

    provider = resolve_provider_name("STEP3_LLM_PROVIDER", "gemini")
    api_key, model, base_url = get_provider_credentials(provider)
    ok, reason, report_text = run_step3(
        write_symbols,
        webhook_url="",
        api_key=api_key,
        model=model,
        benchmark_context=benchmark_context,
        notify=False,
        provider=provider,
        llm_base_url=base_url,
    )
    if not ok:
        raise RuntimeError(f"{trade_day.isoformat()} Step3 failed: {reason}")
    return parse_step3_springboards(report_text, write_symbols, None)


def _build_payloads(target_dates: tuple[date, ...], day_results: list[dict[str, Any]]) -> dict[int, list[dict]]:
    client = create_admin_client()
    target_ints = {int(day.strftime("%Y%m%d")) for day in target_dates}
    rows = fetch_records_from_table(
        client,
        TABLE_RECOMMENDATION_TRACKING,
        "code,recommend_count,recommend_date,initial_price",
    )
    counts, code_dates, first_prices = _history_state(
        [row for row in rows if _int_date(row.get("recommend_date")) not in target_ints]
    )
    payloads: dict[int, list[dict]] = {}
    for result in sorted(day_results, key=lambda item: int(item["recommend_date"])):
        rec_date = int(result["recommend_date"])
        rows_for_date = build_recommendation_payload(rec_date, result["symbols_info"], counts, code_dates, first_prices)
        _apply_ai_marks(rows_for_date, result["ai_codes"], result["springboard_updates"])
        payloads[rec_date] = rows_for_date
        _advance_history(counts, code_dates, first_prices, rows_for_date)
    return payloads


def _history_state(rows: list[dict[str, Any]]) -> tuple[dict[int, int], dict[int, set[int]], dict[int, float]]:
    counts: dict[int, int] = {}
    code_dates: dict[int, set[int]] = {}
    first_dates: dict[int, int] = {}
    first_prices: dict[int, float] = {}
    for row in rows:
        code = _int_code(row.get("code"))
        rec_date = _int_date(row.get("recommend_date"))
        if code is None or rec_date is None:
            continue
        count = _safe_int(row.get("recommend_count"), 1)
        counts[code] = max(counts.get(code, 0), count)
        code_dates.setdefault(code, set()).add(rec_date)
        price = _safe_float(row.get("initial_price"))
        if code not in first_dates or rec_date < first_dates[code]:
            first_dates[code] = rec_date
            first_prices[code] = price
        elif rec_date == first_dates[code] and price > 0:
            first_prices[code] = price
    return counts, code_dates, first_prices


def _advance_history(
    counts: dict[int, int],
    code_dates: dict[int, set[int]],
    first_prices: dict[int, float],
    rows: list[dict],
) -> None:
    for row in rows:
        code = _int_code(row.get("code"))
        rec_date = _int_date(row.get("recommend_date"))
        if code is None or rec_date is None:
            continue
        counts[code] = max(counts.get(code, 0), _safe_int(row.get("recommend_count"), 1))
        code_dates.setdefault(code, set()).add(rec_date)
        price = _safe_float(row.get("initial_price"))
        # days are applied ascending, so the first non-zero price seen is the sticky recommend price
        if price > 0 and first_prices.get(code, 0) <= 0:
            first_prices[code] = price


def _apply_ai_marks(rows: list[dict], ai_codes: list[str], springboard_updates: dict[str, dict[str, Any]]) -> None:
    code_map = ai_code_ints(ai_codes)
    int_to_code6 = {code_int: code6 for code6, code_int in code_map.items()}
    for row in rows:
        code = _int_code(row.get("code"))
        if code is None or code not in int_to_code6:
            continue
        row["is_ai_recommended"] = True
        row.update(springboard_ai_payload(springboard_updates.get(int_to_code6[code])))


def _build_table_rows(day_results: list[dict[str, Any]]) -> dict[str, list[dict]]:
    signal_pending_rows = _simulate_signal_pending_rows(day_results)
    signal_observation_rows: list[dict] = []
    external_seed_rows: list[dict] = []
    market_signal_rows: list[dict] = []
    theme_radar_rows: list[dict] = []
    for result in day_results:
        details = result.get("step2_details") or {}
        trade_date = str(result.get("trade_date") or "")
        benchmark_context = result.get("benchmark_context") or {}
        ai_codes = list(result.get("ai_codes") or [])
        market_signal_rows.append({"trade_date": trade_date, **benchmark_context_payload(benchmark_context)})
        theme_snapshot = _theme_radar_snapshot(details)
        if theme_snapshot:
            theme_radar_rows.append(build_theme_radar_snapshot_row(theme_snapshot))
        signal_observation_rows.extend(_signal_observation_rows(details, benchmark_context, ai_codes, trade_date))
        external_seed_rows.extend((details.get("metrics", {}) or {}).get("external_seed_observation_rows") or [])
    return {
        "signal_pending": signal_pending_rows,
        "signal_observations": signal_observation_rows,
        "external_seed_observations": external_seed_rows,
        "market_signal_daily": market_signal_rows,
        "theme_radar_snapshot": theme_radar_rows,
    }


def _simulate_signal_pending_rows(day_results: list[dict[str, Any]]) -> list[dict]:
    final_rows: dict[int, dict] = {}
    active_ids: list[int] = []
    next_id = 1
    for result in sorted(day_results, key=lambda item: str(item.get("trade_date") or "")):
        trade_date = str(result.get("trade_date") or "")
        for row in _pending_rows_for_day(result):
            row["id"] = next_id
            final_rows[next_id] = row
            active_ids.append(next_id)
            next_id += 1
        active = [final_rows[row_id] for row_id in active_ids]
        updates, _confirmed = run_confirmation_cycle(
            active, (result.get("step2_details") or {}).get("all_df_map") or {}, trade_date
        )
        active_ids = _apply_pending_updates(final_rows, active_ids, updates)
    return [_without_temp_id(row) for row in final_rows.values()]


def _pending_rows_for_day(result: dict[str, Any]) -> list[dict]:
    from core.candidate_metadata import build_candidate_metadata_map, candidate_signal_triggers, merge_trigger_maps
    from workflows.step2_signal_confirmation import build_pending_signal_rows

    details = result.get("step2_details") or {}
    metrics = details.get("metrics", {}) or {}
    candidate_entries = _selected_candidate_entries(details)
    triggers = merge_trigger_maps(details.get("triggers") or {}, candidate_signal_triggers(candidate_entries))
    return build_pending_signal_rows(
        signal_date=str(result.get("trade_date") or ""),
        triggers=triggers,
        df_map=details.get("all_df_map") or {},
        regime=str((result.get("benchmark_context") or {}).get("regime") or "NEUTRAL"),
        name_map=details.get("name_map") or {},
        sector_map=details.get("sector_map") or {},
        candidate_metadata_map=build_candidate_metadata_map(
            candidate_entries, metrics.get("mainline_candidates") or []
        ),
    )


def _selected_candidate_entries(details: dict) -> list[dict]:
    selected = {str(code).strip() for code in details.get("selected_for_ai", []) or [] if str(code).strip()}
    if not selected:
        return []
    return [
        item for item in details.get("candidate_entries", []) or [] if str(item.get("code", "")).strip() in selected
    ]


def _apply_pending_updates(final_rows: dict[int, dict], active_ids: list[int], updates: list[dict]) -> list[int]:
    done_ids: set[int] = set()
    for update in updates:
        row_id = int(update.get("id") or 0)
        row = final_rows.get(row_id)
        if not row:
            continue
        row["status"] = update.get("status", row.get("status"))
        row["days_elapsed"] = update.get("days_elapsed", row.get("days_elapsed", 0))
        row["confirm_reason"] = update.get("confirm_reason", "")
        if update.get("confirm_date"):
            row["confirm_date"] = update["confirm_date"]
        if update.get("expire_date"):
            row["expire_date"] = update["expire_date"]
        if row.get("status") in {"confirmed", "expired"}:
            done_ids.add(row_id)
    return [row_id for row_id in active_ids if row_id not in done_ids]


def _without_temp_id(row: dict) -> dict:
    out = dict(row)
    out.pop("id", None)
    return out


def _signal_observation_rows(
    details: dict, benchmark_context: dict, ai_codes: list[str], trade_date: str
) -> list[dict]:
    if not details:
        return []
    regime = str((benchmark_context or {}).get("regime") or "NEUTRAL")
    rows = signal_observations.build_signal_observation_rows(details, regime, ai_codes, trade_date=trade_date)
    rows.extend(signal_observations.build_shadow_observation_rows(details, regime, trade_date=trade_date))
    rows.extend(signal_observations.build_external_seed_signal_rows(details, regime, trade_date=trade_date))
    return rows


def _theme_radar_snapshot(details: dict) -> dict:
    metrics = (details or {}).get("metrics", {}) or {}
    snapshot = metrics.get("theme_radar_current") or metrics.get("theme_radar") or {}
    return snapshot if isinstance(snapshot, dict) and snapshot.get("trade_date") else {}


def _validate_payloads(payloads: dict[int, list[dict]], *, allow_empty_date: bool) -> None:
    empty_dates = [date_int for date_int, rows in payloads.items() if not rows]
    if empty_dates and not allow_empty_date:
        text = ",".join(str(d) for d in empty_dates)
        raise RuntimeError(f"生成结果存在空日期: {text}；如确认要清空这些日期，请加 --allow-empty-date")


def _fetch_target_rows(client, target_dates: tuple[date, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec_date in [int(day.strftime("%Y%m%d")) for day in target_dates]:
        resp = (
            client.table(TABLE_RECOMMENDATION_TRACKING)
            .select("*")
            .eq("recommend_date", rec_date)
            .order("code", desc=False)
            .execute()
        )
        rows.extend(resp.data or [])
    return rows


def _replace_target_dates(client, payloads: dict[int, list[dict]], old_rows: list[dict[str, Any]]) -> dict[str, Any]:
    upserted = 0
    for rows in payloads.values():
        upsert_recommendation_payload_rows(client, rows)
        upserted += len(rows)
    stale_ids = _stale_old_row_ids(payloads, old_rows)
    for batch in chunked(stale_ids, 500):
        client.table(TABLE_RECOMMENDATION_TRACKING).delete().in_("id", batch).execute()
    return {
        "applied_at": datetime.now(UTC).isoformat(),
        "rows_upserted": upserted,
        "stale_deleted": len(stale_ids),
        "dates": sorted(payloads),
    }


def _replace_auxiliary_tables(
    client, target_dates: tuple[date, ...], table_rows: dict[str, list[dict]]
) -> dict[str, Any]:
    from core.constants import (
        TABLE_EXTERNAL_SEED_OBSERVATIONS,
        TABLE_MARKET_SIGNAL_DAILY,
        TABLE_SIGNAL_OBSERVATIONS,
        TABLE_SIGNAL_PENDING,
        TABLE_THEME_RADAR_SNAPSHOT,
    )

    target_iso = [day.isoformat() for day in target_dates]
    _delete_by_dates(client, TABLE_SIGNAL_PENDING, "signal_date", target_iso)
    _delete_by_dates(client, TABLE_SIGNAL_OBSERVATIONS, "trade_date", target_iso, market="cn")
    _delete_by_dates(client, TABLE_EXTERNAL_SEED_OBSERVATIONS, "trade_date", target_iso, market="cn")
    signal_pending_inserted = _insert_rows(client, TABLE_SIGNAL_PENDING, table_rows.get("signal_pending") or [])
    signal_observations_upserted = upsert_signal_observations(table_rows.get("signal_observations") or [])
    external_seed_upserted = _upsert_rows(
        client,
        TABLE_EXTERNAL_SEED_OBSERVATIONS,
        _rows_with_updated_at(table_rows.get("external_seed_observations") or []),
        "market,trade_date,source,code",
    )
    market_signal_upserted = _upsert_rows(
        client,
        TABLE_MARKET_SIGNAL_DAILY,
        table_rows.get("market_signal_daily") or [],
        "trade_date",
    )
    theme_radar_upserted = _upsert_rows(
        client,
        TABLE_THEME_RADAR_SNAPSHOT,
        table_rows.get("theme_radar_snapshot") or [],
        "trade_date",
    )
    return {
        "signal_pending_inserted": signal_pending_inserted,
        "signal_observations_upserted": signal_observations_upserted,
        "external_seed_upserted": external_seed_upserted,
        "market_signal_upserted": market_signal_upserted,
        "theme_radar_upserted": theme_radar_upserted,
    }


def _rows_with_updated_at(rows: list[dict]) -> list[dict]:
    now_iso = datetime.now(UTC).isoformat()
    return [{**row, "updated_at": row.get("updated_at") or now_iso} for row in rows]


def _delete_by_dates(
    client, table: str, date_column: str, target_dates: list[str], *, market: str | None = None
) -> None:
    for batch in chunked(target_dates, 50):
        query = client.table(table).delete().in_(date_column, batch)
        if market:
            query = query.eq("market", market)
        query.execute()


def _insert_rows(client, table: str, rows: list[dict]) -> int:
    inserted = 0
    for batch in chunked(rows, 500):
        if batch:
            client.table(table).insert(batch).execute()
            inserted += len(batch)
    return inserted


def _upsert_rows(client, table: str, rows: list[dict], conflict: str) -> int:
    upserted = 0
    for batch in chunked(rows, 500):
        if batch:
            client.table(table).upsert(batch, on_conflict=conflict).execute()
            upserted += len(batch)
    return upserted


def _stale_old_row_ids(payloads: dict[int, list[dict]], old_rows: list[dict[str, Any]]) -> list[Any]:
    codes_by_date = {
        rec_date: {_int_code(row.get("code")) for row in rows if _int_code(row.get("code")) is not None}
        for rec_date, rows in payloads.items()
    }
    stale = []
    for row in old_rows:
        rec_date = _int_date(row.get("recommend_date"))
        code = _int_code(row.get("code"))
        if rec_date not in codes_by_date or code in codes_by_date[rec_date]:
            continue
        row_id = row.get("id")
        if row_id is not None:
            stale.append(row_id)
    return stale


def _refresh_performance() -> None:
    summary = refresh_tracking_performance("cn", max_dates=30, kline_count=160)
    print(
        "[recommendation-backfill] performance refreshed: "
        f"rows_updated={summary.get('rows_updated', 0)}, latest_trade_date={summary.get('latest_trade_date', '')}"
    )


def _write_artifacts(
    output_dir: Path,
    target_dates: tuple[date, ...],
    day_results: list[dict[str, Any]],
    payloads: dict[int, list[dict]],
    old_rows: list[dict[str, Any]],
    table_rows: dict[str, list[dict]],
) -> None:
    _write_json(output_dir / "summary.json", _summary(target_dates, day_results, payloads, old_rows))
    _write_json(output_dir / "old_rows_backup.json", recommendation_backup_rows(old_rows, ai_codes=None))
    for rec_date, rows in payloads.items():
        _write_json(output_dir / f"recommendation_tracking_{rec_date}.json", recommendation_backup_rows(rows, None))
    _write_json(output_dir / "table_row_counts.json", {key: len(value) for key, value in table_rows.items()})
    _write_json(output_dir / "signal_pending_rows.json", table_rows.get("signal_pending") or [])


def _summary(
    target_dates: tuple[date, ...],
    day_results: list[dict[str, Any]],
    payloads: dict[int, list[dict]],
    old_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    old_count_by_date: dict[int, int] = {}
    for row in old_rows:
        rec_date = _int_date(row.get("recommend_date"))
        if rec_date is not None:
            old_count_by_date[rec_date] = old_count_by_date.get(rec_date, 0) + 1
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "replay_context": {
            "purpose": "operational_candidate_refresh",
            "price_data": "historical_as_of_trade_date",
            "metadata": "current_snapshot",
            "financial_metrics_requested": False,
            "dynamic_policy": "off",
            "point_in_time_backtest_safe": False,
        },
        "target_dates": [day.isoformat() for day in target_dates],
        "days": [
            {
                "recommend_date": int(result["recommend_date"]),
                "raw_count": result["raw_count"],
                "write_count": result["write_count"],
                "payload_count": len(payloads.get(int(result["recommend_date"]), [])),
                "old_count": old_count_by_date.get(int(result["recommend_date"]), 0),
                "ai_count": len(result["ai_codes"]),
            }
            for result in day_results
        ],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    return str(value)


@contextmanager
def _day_env(trade_day: date, skip_step3: bool) -> Iterator[None]:
    overrides = {
        "END_CALENDAR_DAY": trade_day.isoformat(),
        "FUNNEL_SKIP_FINANCIAL_METRICS": "1",
        "FUNNEL_DYNAMIC_POLICY": "off",
        WRITE_CONTEXT_ENV: CLI_WRITE_CONTEXT,
        "STEP3_ENFORCE_TARGET_TRADE_DATE": "1",
    }
    if skip_step3:
        overrides["STEP3_SKIP_LLM"] = "1"
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float:
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return price if price > 0 else 0.0


def _int_date(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_code(value: Any) -> int | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(digits) if digits else None
