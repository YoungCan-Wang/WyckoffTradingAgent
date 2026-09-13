"""ashare-mainline-radar 项目库客户端。与威科夫 Supabase 凭证隔离，禁止混用。

本仓原先只有自己的 theme_radar_snapshot，读的是威科夫项目。Radar 在另一个
Supabase（策划 25 只篮子），必须单独 URL/Key。缺凭证时 fail-closed：明确报错，
不得当成「今日无交叉」静默跳过。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from integrations.supabase_base import close_client

logger = logging.getLogger(__name__)

RADAR_URL_ENVS = ("RADAR_SUPABASE_URL", "MAINLINE_RADAR_SUPABASE_URL")
RADAR_KEY_ENVS = (
    "RADAR_SUPABASE_SERVICE_ROLE_KEY",
    "RADAR_SUPABASE_SERVICE_KEY",
    "MAINLINE_RADAR_SUPABASE_SERVICE_ROLE_KEY",
)
TABLE_THEME = "radar_theme_snapshots"
TABLE_SYMBOL = "radar_symbol_snapshots"
TABLE_PLAN = "radar_trade_plans"
TABLE_RUN = "radar_runs"
DATE_COLUMNS = ("market_date", "trade_date")
PAGE = 500


class RadarCredentialsMissing(RuntimeError):
    """Radar 项目库凭证缺失。调用方必须记 ERROR，不能当空集。"""


class RadarFetchError(RuntimeError):
    """Radar 表读取失败。调用方必须记 ERROR，不能当空集。"""


@dataclass(frozen=True)
class RadarDaySnapshot:
    trade_date: str
    themes: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    plan_codes: list[str] = field(default_factory=list)
    run_id: str | None = None


def radar_env_help() -> str:
    return "需要 RADAR_SUPABASE_URL 与 RADAR_SUPABASE_SERVICE_ROLE_KEY（或 RADAR_SUPABASE_SERVICE_KEY）"


def resolve_radar_credentials() -> tuple[str, str]:
    url = _first_env(RADAR_URL_ENVS)
    key = _first_env(RADAR_KEY_ENVS)
    if not url or not key:
        raise RadarCredentialsMissing(radar_env_help())
    return url, key


def radar_configured() -> bool:
    try:
        resolve_radar_credentials()
    except RadarCredentialsMissing:
        return False
    return True


def load_radar_day(trade_date: str) -> RadarDaySnapshot:
    """读取某一交易日的主题、成分股与交易计划。缺凭证或读失败都抛错。"""
    date_text = str(trade_date or "").strip()
    if not date_text:
        raise RadarFetchError("trade_date 为空")
    client = create_radar_client()
    try:
        run_id = _latest_run_id(client, date_text)
        themes = _fetch_dated_rows(client, TABLE_THEME, date_text, run_id)
        symbols = _fetch_dated_rows(client, TABLE_SYMBOL, date_text, run_id)
        plans = _fetch_dated_rows(client, TABLE_PLAN, date_text, run_id)
    except RadarCredentialsMissing:
        raise
    except Exception as exc:
        raise RadarFetchError(f"{date_text}: {exc}") from exc
    finally:
        close_client(client)
    return RadarDaySnapshot(
        trade_date=date_text,
        themes=themes,
        symbols=symbols,
        plan_codes=_plan_codes(plans),
        run_id=run_id,
    )


def create_radar_client():
    from supabase import create_client

    from integrations.supabase_base import _client_options

    url, key = resolve_radar_credentials()
    return create_client(url, key, options=_client_options())


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _latest_run_id(client: Any, trade_date: str) -> str | None:
    for column in DATE_COLUMNS:
        rows = _try_select(client, TABLE_RUN, column, trade_date, fields="id,created_at")
        if rows is None:
            continue
        if not rows:
            return None
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        run_id = rows[0].get("id")
        return str(run_id) if run_id not in (None, "") else None
    return None


def _fetch_dated_rows(client: Any, table: str, trade_date: str, run_id: str | None) -> list[dict[str, Any]]:
    if run_id:
        by_run = _try_select(client, table, "run_id", run_id)
        if by_run:
            return by_run
    for column in DATE_COLUMNS:
        rows = _try_select(client, table, column, trade_date)
        if rows is not None:
            return rows
    raise RadarFetchError(f"{table} 无 market_date/trade_date 可过滤")


def _try_select(client: Any, table: str, column: str, value: str, *, fields: str = "*") -> list[dict[str, Any]] | None:
    try:
        return _paged_eq(client, table, column, value, fields=fields)
    except Exception as exc:
        if _unknown_column(exc, column) or _unknown_table(exc, table):
            return None
        raise


def _paged_eq(client: Any, table: str, column: str, value: str, *, fields: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = client.table(table).select(fields).eq(column, value).range(offset, offset + PAGE - 1).execute()
        batch = list(resp.data or [])
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def _plan_codes(rows: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for row in rows:
        raw = row.get("symbol") or row.get("ts_code") or row.get("code")
        text = str(raw or "").strip()
        if text:
            codes.append(text)
    return codes


def _unknown_column(exc: Exception, column: str) -> bool:
    text = str(exc).lower()
    return column.lower() in text and any(token in text for token in ("column", "could not find", "42703", "pgrst"))


def _unknown_table(exc: Exception, table: str) -> bool:
    text = str(exc).lower()
    return table.lower() in text and any(
        token in text for token in ("schema cache", "does not exist", "42p01", "pgrst")
    )
