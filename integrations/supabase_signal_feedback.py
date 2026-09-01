"""Supabase read/write helpers for signal feedback tables."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from core.constants import (
    TABLE_SIGNAL_HEALTH_DAILY,
    TABLE_SIGNAL_OBSERVATIONS,
    TABLE_SIGNAL_OUTCOMES,
    TABLE_SIGNAL_POLICY_SHADOW_RUNS,
    TABLE_SIGNAL_REGISTRY,
)
from core.signal_policy_shadow_schema import column_names as _SHADOW_OPTIONAL_COLUMNS_FN
from integrations.supabase_base import close_client as _close
from integrations.supabase_base import create_admin_client as _admin
from integrations.supabase_base import is_admin_configured as _configured
from integrations.supabase_base import require_server_write_context

logger = logging.getLogger(__name__)

OPTIONAL_SIGNAL_OBSERVATION_COLUMNS = (
    "profile_tag",
    "stage_tag",
    "trigger_tags",
    "selection_mode",
    "policy_version",
    "candidate_rank",
    "features_json",
    "strategy_version",
    "candidate_lane",
    "entry_type",
    "signal_key",
    "candidate_status",
)

# 缺列时可以牺牲的列，按表登记。命中就剔掉重试，只丢字段不丢行。
#
# 为什么要扩到 shadow_runs：这层降级原先只保 signal_observations，别的表缺列直接丢整行。
# 2026-07-04 ``_policy_shadow_row`` 新增 attribution_signal_weights /
# attribution_policy_meta 两个键而生产表没跟上，此后每次 upsert 都 42703；又因
# ``upsert_policy_shadow_run`` 是 raise_on_error=False，异常只进 logger.warning，
# 影子账本停在 2026-07-01 整两个月无人发现，归因重算一直误报 insufficient_shadow_sample。
# 补 DDL（core/signal_policy_shadow_schema.py）治本，这里治「下次再漂移」。
OPTIONAL_COLUMNS_BY_TABLE: dict[str, tuple[str, ...]] = {
    TABLE_SIGNAL_OBSERVATIONS: OPTIONAL_SIGNAL_OBSERVATION_COLUMNS,
    TABLE_SIGNAL_POLICY_SHADOW_RUNS: tuple(sorted(_SHADOW_OPTIONAL_COLUMNS_FN())),
}


def _recent_cutoff(days: int) -> str:
    return (date.today() - timedelta(days=max(int(days), 1))).isoformat()


def _fetch_paginated(query_factory, limit: int, page_size: int = 1000) -> list[dict[str, Any]]:
    """按 .range() 分页拉取，直到达到 limit 或数据取尽。

    PostgREST 服务端对单次请求有硬性行数上限（常见默认 1000），客户端传更大的
    ``.limit()`` 并不能突破它——超过上限时会被静默截断，且截断发生在按
    trade_date 倒序排序之后，等价于"只看最近一批、其余全部丢失"。这会让
    信号健康度统计长期基于失真的小样本，看不出信号已经变差。
    """
    page = max(min(int(page_size), 1000), 1)
    rows: list[dict[str, Any]] = []
    start = 0
    while len(rows) < limit:
        remaining = limit - len(rows)
        stop = start + min(page, remaining) - 1
        batch = query_factory().range(start, stop).execute().data or []
        rows.extend(batch)
        if len(batch) < min(page, remaining):
            break
        start += len(batch)
    return rows


def _looks_like_schema_miss(exc: Exception) -> bool:
    text = str(exc).lower()
    return "column" in text or "schema cache" in text or "could not find" in text


def _drop_optional_columns(table: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """剔掉该表登记的可选列，返回 (新行, 实际剔掉的列)。

    返回剔掉了哪些列是为了让调用方 warn 出来：静默降级正是影子账本停摆两个月
    没被发现的原因。
    """
    optional = OPTIONAL_COLUMNS_BY_TABLE.get(table, ())
    if not optional:
        return rows, []
    dropped = sorted({column for row in rows for column in optional if column in row})
    if not dropped:
        return rows, []
    clean = [{key: value for key, value in row.items() if key not in set(dropped)} for row in rows]
    return clean, dropped


def _execute_upsert(
    table: str,
    rows: list[dict[str, Any]],
    conflict: str,
    *,
    raise_on_error: bool = True,
) -> int:
    if not _configured() or not rows:
        return 0
    require_server_write_context(f"upsert {table}")
    client = None
    try:
        client = _admin()
        try:
            client.table(table).upsert(rows, on_conflict=conflict).execute()
        except Exception as exc:
            if not _looks_like_schema_miss(exc):
                raise
            compatible, dropped = _drop_optional_columns(table, rows)
            if not dropped:
                raise
            logger.warning(
                "%s missing optional columns; retried without %s (run scripts/print_*_ddl.py and apply)",
                table,
                dropped,
            )
            client.table(table).upsert(compatible, on_conflict=conflict).execute()
        return len(rows)
    except Exception as exc:
        logger.warning("upsert %s failed: %s", table, exc)
        if raise_on_error:
            raise
        return 0
    finally:
        if client is not None:
            _close(client)


def upsert_signal_observations(rows: list[dict[str, Any]]) -> int:
    return _execute_upsert(TABLE_SIGNAL_OBSERVATIONS, rows, "market,trade_date,code,signal_type")


def upsert_signal_outcomes(rows: list[dict[str, Any]]) -> int:
    return _execute_upsert(TABLE_SIGNAL_OUTCOMES, rows, "observation_id,horizon_days")


def upsert_signal_registry(rows: list[dict[str, Any]]) -> int:
    return _execute_upsert(TABLE_SIGNAL_REGISTRY, rows, "market,signal_type,regime")


def replace_signal_health(
    rows: list[dict[str, Any]],
    *,
    market: str,
    as_of_date: str,
    allow_empty: bool = False,
) -> int:
    return _replace_derived_rows(
        TABLE_SIGNAL_HEALTH_DAILY,
        rows,
        {"market": market, "as_of_date": as_of_date},
        "market,as_of_date,signal_type,regime,horizon_days",
        allow_empty=allow_empty,
    )


def replace_signal_registry(rows: list[dict[str, Any]], *, market: str, allow_empty: bool = False) -> int:
    return _replace_derived_rows(
        TABLE_SIGNAL_REGISTRY,
        rows,
        {"market": market},
        "market,signal_type,regime",
        allow_empty=allow_empty,
    )


def _conflict_columns(conflict: str) -> list[str]:
    return [part.strip() for part in conflict.split(",") if part.strip()]


def _conflict_key(row: dict[str, Any], conflict: str) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in _conflict_columns(conflict))


def _apply_filters(query: Any, filters: dict[str, str]) -> Any:
    for column, value in filters.items():
        query = query.eq(column, value)
    return query


def _delete_orphaned_rows(client: Any, table: str, filters: dict[str, str], conflict: str, rows: list[dict]) -> None:
    """Remove filter-scoped rows whose conflict keys are absent from the new snapshot."""
    keep_keys = {_conflict_key(row, conflict) for row in rows}
    columns = ",".join(_conflict_columns(conflict))
    existing = _apply_filters(client.table(table).select(columns or "*"), filters).execute().data or []
    for old in existing:
        if _conflict_key(old, conflict) in keep_keys:
            continue
        query = client.table(table).delete()
        query = _apply_filters(query, filters)
        for column in _conflict_columns(conflict):
            query = query.eq(column, old.get(column))
        query.execute()


def _replace_derived_rows(
    table: str,
    rows: list[dict[str, Any]],
    filters: dict[str, str],
    conflict: str,
    *,
    allow_empty: bool,
) -> int:
    if not _configured():
        return 0
    if not rows and not allow_empty:
        logger.warning("skip replacing %s with an empty snapshot", table)
        return 0
    require_server_write_context(f"replace {table}")
    client = _admin()
    try:
        # Upsert before orphan delete: if the write fails, previous rows remain
        # (empty registry fail-opens dynamic policy to admit all triggers).
        if rows:
            client.table(table).upsert(rows, on_conflict=conflict).execute()
        _delete_orphaned_rows(client, table, filters, conflict, rows)
        return len(rows)
    finally:
        _close(client)


def upsert_policy_shadow_run(row: dict[str, Any]) -> int:
    return _execute_upsert(TABLE_SIGNAL_POLICY_SHADOW_RUNS, [row], "market,trade_date", raise_on_error=False)


def load_recent_signal_observations(days: int = 90, limit: int = 5000, market: str = "cn") -> list[dict[str, Any]]:
    if not _configured():
        return []
    client = None
    try:
        client = _admin()

        def _query():
            return (
                client.table(TABLE_SIGNAL_OBSERVATIONS)
                .select("*")
                .eq("market", market)
                .gte("trade_date", _recent_cutoff(days))
                .order("trade_date", desc=True)
                .order("id", desc=True)
            )

        return _fetch_paginated(_query, max(int(limit), 1))
    except Exception as exc:
        logger.warning("load observations failed: %s", exc)
        return []
    finally:
        if client is not None:
            _close(client)


def load_pending_outcome_observation_ids(limit: int = 20000, market: str = "cn") -> list[int]:
    """取出所有仍卡在 pending 的 observation_id，不受滚动时间窗限制。

    outcome 结算依赖未来 K 线数据补齐，触发很久之前的信号一旦滑出
    ``observation_days`` 窗口就再也不会被重新拉取结算，导致 pending 记录
    永久卡死。这里单独按 outcome 表本身找出待结算的 observation，交给
    refresh_outcomes 补跑。
    """
    if not _configured():
        return []
    client = None
    try:
        client = _admin()

        def _query():
            return (
                client.table(TABLE_SIGNAL_OUTCOMES)
                .select("observation_id")
                .eq("market", market)
                .eq("status", "pending")
                .order("id", desc=True)
            )

        rows = _fetch_paginated(_query, max(int(limit), 1))
        ids: set[int] = set()
        for row in rows:
            try:
                ids.add(int(row.get("observation_id")))
            except (TypeError, ValueError):
                continue
        return sorted(ids)
    except Exception as exc:
        logger.warning("load pending outcome ids failed: %s", exc)
        return []
    finally:
        if client is not None:
            _close(client)


def load_signal_observations_by_ids(observation_ids: list[int], market: str = "cn") -> list[dict[str, Any]]:
    if not _configured() or not observation_ids:
        return []
    client = None
    try:
        client = _admin()
        rows: list[dict[str, Any]] = []
        for chunk_start in range(0, len(observation_ids), 500):
            chunk = observation_ids[chunk_start : chunk_start + 500]
            resp = client.table(TABLE_SIGNAL_OBSERVATIONS).select("*").eq("market", market).in_("id", chunk).execute()
            rows.extend(resp.data or [])
        return rows
    except Exception as exc:
        logger.warning("load observations by ids failed: %s", exc)
        return []
    finally:
        if client is not None:
            _close(client)


def load_signal_outcome_states(
    observation_ids: list[int],
    market: str = "cn",
    *,
    raise_on_error: bool = False,
) -> dict[int, dict[int, str]]:
    """Load settlement state without reading full outcome payloads."""
    if not _configured():
        if raise_on_error:
            raise RuntimeError("Supabase signal feedback is not configured")
        return {}
    if not observation_ids:
        return {}
    client = None
    try:
        client = _admin()
        states: dict[int, dict[int, str]] = {}
        for chunk_start in range(0, len(observation_ids), 100):
            chunk = observation_ids[chunk_start : chunk_start + 100]
            rows = (
                client.table(TABLE_SIGNAL_OUTCOMES)
                .select("observation_id,horizon_days,status")
                .eq("market", market)
                .in_("observation_id", chunk)
                .execute()
                .data
                or []
            )
            for row in rows:
                try:
                    observation_id = int(row.get("observation_id"))
                    horizon = int(row.get("horizon_days"))
                except (TypeError, ValueError):
                    continue
                states.setdefault(observation_id, {})[horizon] = str(row.get("status") or "")
        return states
    except Exception as exc:
        logger.warning("load outcome states failed: %s", exc)
        if raise_on_error:
            raise RuntimeError("failed to load signal outcome states") from exc
        return {}
    finally:
        if client is not None:
            _close(client)


def load_recent_signal_outcomes(
    days: int = 180,
    limit: int = 20000,
    market: str = "cn",
    *,
    raise_on_error: bool = False,
) -> list[dict[str, Any]]:
    if not _configured():
        if raise_on_error:
            raise RuntimeError("Supabase signal feedback is not configured")
        return []
    client = None
    try:
        client = _admin()

        def _query():
            return (
                client.table(TABLE_SIGNAL_OUTCOMES)
                .select("*")
                .eq("market", market)
                .gte("trade_date", _recent_cutoff(days))
                .order("trade_date", desc=True)
                .order("id", desc=True)
            )

        return _fetch_paginated(_query, max(int(limit), 1))
    except Exception as exc:
        logger.warning("load outcomes failed: %s", exc)
        if raise_on_error:
            raise RuntimeError("failed to load signal outcomes") from exc
        return []
    finally:
        if client is not None:
            _close(client)


def _latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_date = ""
    selected: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        row_date = str(row.get("as_of_date") or "")
        if row_date > latest_date:
            latest_date = row_date
            selected = {}
        if row_date == latest_date:
            key = (str(row.get("signal_type")), str(row.get("regime")), int(row.get("horizon_days") or 0))
            selected[key] = row
    return list(selected.values())


def load_signal_health_snapshot(market: str = "cn", limit: int = 1000) -> list[dict[str, Any]]:
    if not _configured():
        return []
    client = None
    try:
        client = _admin()
        resp = (
            client.table(TABLE_SIGNAL_HEALTH_DAILY)
            .select("*")
            .eq("market", market)
            .order("as_of_date", desc=True)
            .limit(max(int(limit), 1))
            .execute()
        )
        return _latest_rows(resp.data or [])
    except Exception as exc:
        logger.warning("load health failed: %s", exc)
        return []
    finally:
        if client is not None:
            _close(client)


def load_signal_registry(market: str = "cn", *, raise_on_error: bool = False) -> list[dict[str, Any]]:
    if not _configured():
        if raise_on_error:
            raise RuntimeError("Supabase signal feedback is not configured")
        return []
    client = None
    try:
        client = _admin()
        resp = client.table(TABLE_SIGNAL_REGISTRY).select("*").eq("market", market).execute()
        return resp.data or []
    except Exception as exc:
        logger.warning("load registry failed: %s", exc)
        if raise_on_error:
            raise RuntimeError("failed to load signal registry") from exc
        return []
    finally:
        if client is not None:
            _close(client)


def load_policy_shadow_runs(days: int = 30, limit: int = 1000, market: str = "cn") -> list[dict[str, Any]]:
    if not _configured():
        return []
    client = None
    try:
        client = _admin()
        resp = (
            client.table(TABLE_SIGNAL_POLICY_SHADOW_RUNS)
            .select("*")
            .eq("market", market)
            .gte("trade_date", _recent_cutoff(days))
            .order("trade_date", desc=True)
            .limit(max(int(limit), 1))
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.warning("load policy shadow failed: %s", exc)
        return []
    finally:
        if client is not None:
            _close(client)
