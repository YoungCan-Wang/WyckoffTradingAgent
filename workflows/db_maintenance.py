"""Database maintenance workflow for expiring old Supabase rows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.constants import (
    TABLE_DAILY_NAV,
    TABLE_EXTERNAL_SEED_OBSERVATIONS,
    TABLE_FACTOR_IC_DAILY,
    TABLE_MARKET_SIGNAL_DAILY,
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_RECOMMENDATION_TRACKING_HK,
    TABLE_RECOMMENDATION_TRACKING_US,
    TABLE_SIGNAL_PENDING,
    TABLE_TRADE_ORDERS,
)
from integrations.supabase_base import create_admin_client


@dataclass(frozen=True)
class DbMaintenanceRequest:
    dry_run: bool = False


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(float(raw)), minimum)
    except (TypeError, ValueError):
        return default


# (table, date_column, ttl_days, cutoff_kind)
# cutoff_kind:
# - iso_date:      YYYY-MM-DD（字符串日期列）
# - yyyymmdd_int:  YYYYMMDD（整数日期列）
CLEANUP_RULES: list[tuple[str, str, int, str]] = [
    (TABLE_TRADE_ORDERS, "trade_date", 15, "iso_date"),
    (TABLE_SIGNAL_PENDING, "signal_date", _int_env("DB_SIGNAL_PENDING_RETENTION_DAYS", 30), "iso_date"),
    (TABLE_MARKET_SIGNAL_DAILY, "trade_date", _int_env("DB_MARKET_SIGNAL_RETENTION_DAYS", 180), "iso_date"),
    (TABLE_DAILY_NAV, "trade_date", 15, "iso_date"),
    (TABLE_EXTERNAL_SEED_OBSERVATIONS, "trade_date", _int_env("FUNNEL_EXTERNAL_SEED_RETENTION_DAYS", 180), "iso_date"),
    # 因子 IC 每周一行/因子，量很小；留 365 天以便观察方向是否跨年稳定——
    # 该表的价值恰在长期趋势，删太早就失去意义。
    (TABLE_FACTOR_IC_DAILY, "eval_date", _int_env("DB_FACTOR_IC_RETENTION_DAYS", 365), "iso_date"),
]
RECOMMENDATION_KEEP_DATES = _int_env("DB_RECOMMENDATION_KEEP_DATES", 30)
RECOMMENDATION_DATE_PAGE_SIZE = 1000
RECOMMENDATION_TRACKING_TABLES = (
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_RECOMMENDATION_TRACKING_US,
    TABLE_RECOMMENDATION_TRACKING_HK,
)


def run_db_maintenance(request: DbMaintenanceRequest) -> int:
    client = create_admin_client()
    all_ok = True
    for table, date_col, ttl_days, cutoff_kind in CLEANUP_RULES:
        status, count = cleanup_table(client, table, date_col, ttl_days, cutoff_kind, dry_run=request.dry_run)
        print(_table_status_line(table, status, count, ttl_days=ttl_days))
        if status.startswith("error"):
            all_ok = False

    for table in RECOMMENDATION_TRACKING_TABLES:
        status, count = cleanup_recommendation_table(client, table, dry_run=request.dry_run)
        print(_table_status_line(table, status, count))
        if status.startswith("error"):
            all_ok = False
    return 0 if all_ok else 1


def cleanup_table(
    client,
    table: str,
    date_col: str,
    ttl_days: int,
    cutoff_kind: str,
    *,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    cutoff = _cutoff_value(ttl_days, cutoff_kind)
    try:
        if dry_run:
            resp = client.table(table).select("*", count="exact").lt(date_col, cutoff).limit(0).execute()
            return "dry_run", resp.count or 0
        client.table(table).delete().lt(date_col, cutoff).execute()
        return "ok", None
    except Exception as e:
        return f"error: {e}", None


def cleanup_recommendation_table(
    client,
    table: str,
    *,
    keep_dates: int = RECOMMENDATION_KEEP_DATES,
    page_size: int = RECOMMENDATION_DATE_PAGE_SIZE,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    keep_dates = max(int(keep_dates), 1)
    page_size = max(int(page_size), 1)
    dates = _latest_recommend_dates(client, table, keep_dates, page_size)
    if len(dates) < keep_dates:
        count = 0 if dry_run else None
        return f"keep_all, keep_dates={keep_dates}, distinct_dates={len(dates)}", count

    cutoff = dates[keep_dates - 1]
    try:
        if dry_run:
            resp = client.table(table).select("*", count="exact").lt("recommend_date", cutoff).limit(0).execute()
            return f"dry_run, keep_dates={keep_dates}, cutoff={cutoff}", resp.count or 0
        client.table(table).delete().lt("recommend_date", cutoff).execute()
        return f"ok, keep_dates={keep_dates}, cutoff={cutoff}", None
    except Exception as e:
        return f"error: {e}", None


def cleanup_recommendation_tracking(
    client,
    *,
    keep_dates: int = RECOMMENDATION_KEEP_DATES,
    page_size: int = RECOMMENDATION_DATE_PAGE_SIZE,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    return cleanup_recommendation_table(
        client,
        TABLE_RECOMMENDATION_TRACKING,
        keep_dates=keep_dates,
        page_size=page_size,
        dry_run=dry_run,
    )


def _cutoff_value(ttl_days: int, kind: str) -> str | int:
    d = (datetime.now(UTC) - timedelta(days=ttl_days)).date()
    if kind == "yyyymmdd_int":
        return int(d.strftime("%Y%m%d"))
    return d.isoformat()


def _latest_recommend_dates(client, table: str, keep_dates: int, page_size: int) -> list[int]:
    dates: list[int] = []
    seen: set[int] = set()
    before_date: int | None = None

    while len(dates) < keep_dates:
        query = client.table(table).select("recommend_date").order("recommend_date", desc=True).limit(page_size)
        if before_date is not None:
            query = query.lt("recommend_date", before_date)

        rows = query.execute().data or []
        valid_dates = [d for row in rows if (d := _to_int_date(row.get("recommend_date"))) is not None]
        if not valid_dates:
            break

        for recommend_date in valid_dates:
            if recommend_date not in seen:
                seen.add(recommend_date)
                dates.append(recommend_date)
                if len(dates) >= keep_dates:
                    break

        before_date = min(valid_dates)

    return dates


def _to_int_date(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _table_status_line(table: str, status: str, count: int | None, *, ttl_days: int | None = None) -> str:
    ttl_text = f", ttl={ttl_days}d" if ttl_days is not None else ""
    suffix = f" ({count} rows)" if count is not None else ""
    return f"[db_maintenance] {table}: {status}{ttl_text}{suffix}"
