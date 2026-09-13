"""日频交叉筛选编排：算交集、挂到漏斗 metrics、盘后落库。

计算可以在出卡前做（给人看）；落库放在 recommendation_tracking 写入之后。
缺 Radar 凭证必须打 ERROR，不得写成「今日无交叉」。
"""

from __future__ import annotations

import logging
from typing import Any

from core.theme_structure_cross import (
    COHORT_PRIMARY,
    collect_wyckoff_views,
    intersect_cross_rows,
)
from integrations.radar_supabase import RadarCredentialsMissing, RadarFetchError, load_radar_day

logger = logging.getLogger(__name__)


def attach_theme_structure_cross(metrics: dict[str, Any]) -> dict[str, Any]:
    """给漏斗卡用：只计算，不写库。"""
    payload = compute_theme_structure_cross(metrics)
    metrics["theme_structure_cross"] = payload
    _log_cross_result(payload, persist=False)
    return payload


def persist_daily_theme_structure_cross(
    *,
    trade_date: str,
    metrics: dict[str, Any] | None,
    extra_rows: list[dict[str, Any]] | None = None,
    dry_run: bool,
    log_fn,
    logs_path: str | None,
) -> dict[str, Any]:
    payload = compute_theme_structure_cross(metrics or {}, extra_rows=extra_rows, trade_date=trade_date)
    if metrics is not None:
        metrics["theme_structure_cross"] = payload
    if dry_run:
        log_fn(f"预演模式: 跳过主线×威科夫交叉写库 status={payload['status']}", logs_path)
        return payload
    written = _persist_if_ready(payload)
    log_fn(
        f"主线×威科夫交叉: status={payload['status']} primary={payload['primary_count']} "
        f"broad={payload['broad_count']} written={written}",
        logs_path,
    )
    return payload


def compute_theme_structure_cross(
    metrics: dict[str, Any],
    *,
    extra_rows: list[dict[str, Any]] | None = None,
    trade_date: str = "",
) -> dict[str, Any]:
    date_text = str(trade_date or metrics.get("end_trade_date") or "").strip()
    wy_rows = collect_wyckoff_views(
        metrics.get("candidate_entries") or [],
        metrics.get("mainline_candidates") or [],
        metrics.get("mainline_candidate_entries") or [],
        extra_rows or [],
    )
    _fill_names(wy_rows, metrics.get("name_map") or {})
    if not date_text:
        return _payload("fetch_failed", [], error="trade_date 为空")
    try:
        radar = load_radar_day(date_text)
    except RadarCredentialsMissing as exc:
        logger.error("[theme-cross] %s", exc)
        return _payload("missing_creds", [], error=str(exc))
    except RadarFetchError as exc:
        logger.error("[theme-cross] %s", exc)
        return _payload("fetch_failed", [], error=str(exc))
    rows = intersect_cross_rows(
        trade_date=date_text,
        wyckoff_rows=wy_rows,
        radar_symbols=radar.symbols,
        radar_themes=radar.themes,
        plan_codes=radar.plan_codes,
    )
    return _payload("ready", rows)


def load_cross_payload_for_report(trade_date: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if fallback and fallback.get("status"):
        return fallback
    try:
        from integrations.supabase_theme_structure_cross import load_theme_structure_cross_rows

        rows = load_theme_structure_cross_rows(trade_date)
    except Exception as exc:
        logger.warning("[theme-cross] 报告回读失败: %s", exc)
        return fallback or {}
    return _payload("ready", rows)


def _persist_if_ready(payload: dict[str, Any]) -> int:
    if payload.get("status") != "ready":
        logger.error("[theme-cross] 未落库：%s %s", payload.get("status"), payload.get("error") or "")
        return 0
    from integrations.supabase_theme_structure_cross import save_theme_structure_cross_rows

    try:
        return save_theme_structure_cross_rows(list(payload.get("rows") or []))
    except Exception as exc:  # noqa: BLE001 - 观察表失败不中断漏斗
        logger.warning("[theme-cross] 落库失败: %s", exc)
        return 0


def _payload(status: str, rows: list[dict[str, Any]], *, error: str = "") -> dict[str, Any]:
    primary = sum(1 for row in rows if row.get("cohort") == COHORT_PRIMARY)
    return {
        "status": status,
        "error": error,
        "rows": rows,
        "primary_count": primary,
        "broad_count": max(len(rows) - primary, 0),
    }


def _fill_names(rows: list[dict[str, Any]], name_map: dict[str, Any]) -> None:
    for row in rows:
        row["name"] = str(row.get("name") or name_map.get(row["ts_code"]) or "").strip()


def _log_cross_result(payload: dict[str, Any], *, persist: bool) -> None:
    suffix = "persist" if persist else "card"
    if payload["status"] != "ready":
        logger.error("[theme-cross] %s status=%s error=%s", suffix, payload["status"], payload.get("error") or "")
        return
    logger.info(
        "[theme-cross] %s ready primary=%d broad=%d",
        suffix,
        payload["primary_count"],
        payload["broad_count"],
    )
