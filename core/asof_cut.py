"""Point-in-time cuts for A-share funnel replay (OHLCV + dated records)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from core.wyckoff_engine import sort_by_date_if_needed

_DATE_KEYS = ("date", "as_of_date", "announce_date", "filed_date", "pub_date", "trade_date")


def parse_as_of(raw: date | datetime | str) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return datetime.fromisoformat(str(raw)[:10]).date()


def cut_ohlcv_to_as_of(df: pd.DataFrame | None, as_of: date | str) -> pd.DataFrame | None:
    """Keep bars on or before as_of. Require an exact as_of bar so a halt cannot impersonate."""
    if df is None or df.empty or "date" not in df.columns:
        return None
    target = parse_as_of(as_of)
    work = sort_by_date_if_needed(df).copy()
    stamps = pd.to_datetime(work["date"], errors="coerce")
    work = work[stamps.dt.date <= target]
    if work.empty:
        return None
    last = pd.to_datetime(work["date"], errors="coerce").dt.date.iloc[-1]
    return work.reset_index(drop=True) if last == target else None


def cut_dated_records(
    records: list[dict[str, Any]] | None,
    as_of: date | str,
    *,
    drop_undated: bool = True,
) -> list[dict[str, Any]]:
    target = parse_as_of(as_of)
    kept: list[dict[str, Any]] = []
    for item in records or []:
        item_date = _record_date(item)
        if item_date is None:
            if not drop_undated:
                kept.append(item)
            continue
        if item_date <= target:
            kept.append(item)
    return kept


def cut_financial_map(
    financial_map: dict[str, dict[str, Any]] | None,
    as_of: date | str,
    *,
    drop_undated: bool = True,
) -> dict[str, dict[str, Any]]:
    target = parse_as_of(as_of)
    out: dict[str, dict[str, Any]] = {}
    for code, payload in (financial_map or {}).items():
        item_date = _record_date(payload)
        if item_date is None:
            if not drop_undated:
                out[code] = payload
            continue
        if item_date <= target:
            out[code] = payload
    return out


def cut_ohlcv_map(df_map: dict[str, pd.DataFrame], as_of: date | str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for symbol, frame in df_map.items():
        cut = cut_ohlcv_to_as_of(frame, as_of)
        if cut is not None:
            out[symbol] = cut
    return out


def _record_date(payload: dict[str, Any]) -> date | None:
    for key in _DATE_KEYS:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            return parse_as_of(raw)
        except (TypeError, ValueError):
            continue
    return None
