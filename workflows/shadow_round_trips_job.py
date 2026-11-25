"""盘后给影子账本的已清仓回合补「持有区间最高/最低价」，写进清仓那笔 sell 的 payload。

自愈：每次都扫全部流水，只补 payload 里还没有区间高低的回合；拉不到日线的回合
先写买卖价、高低留空，下一个交易日再试。已补齐的回合不再拉数据。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd

from core.shadow_ledger import SHADOW_ACCOUNT_ID
from core.shadow_round_trips import (
    ROUND_TRIP_PAYLOAD_KEY,
    ShadowRoundTrip,
    closed_round_trips,
    interval_high_low,
    needs_round_trip_payload,
    round_trip_payload,
)

logger = logging.getLogger(__name__)

BarFetcher = Callable[[str, date, date], tuple[pd.DataFrame | None, str]]


def annotate_closed_round_trips(
    events: list[dict[str, Any]],
    *,
    fetch_bars: BarFetcher,
    write_payload: Callable[[str, dict[str, Any], dict[str, Any]], bool],
) -> dict[str, int]:
    """返回 {closed, written, missing_bars, unmatched}；只写仍缺区间高低的回合。"""
    payload_by_key = {str(row.get("event_key") or ""): row.get("payload") for row in events}
    stats = {"closed": 0, "written": 0, "missing_bars": 0, "unmatched": 0}
    for trip in closed_round_trips(events):
        stats["closed"] += 1
        current = payload_by_key.get(trip.sell_event_key)
        if not trip.sell_event_key or not needs_round_trip_payload(current):
            continue
        record = _round_trip_record(trip, fetch_bars)
        if record["interval_high"] is None:
            stats["missing_bars"] += 1
        patch = {ROUND_TRIP_PAYLOAD_KEY: record}
        if write_payload(trip.sell_event_key, patch, current if isinstance(current, dict) else {}):
            stats["written"] += 1
        else:
            stats["unmatched"] += 1
    return stats


def fetch_unadjusted_daily(code: str, start: date, end: date) -> tuple[pd.DataFrame | None, str]:
    """只走能给不复权日线的源；统一门面的 tushare/baostock 兜底会静默给前复权价。"""
    from integrations.data_source_akshare import fetch_stock_akshare
    from integrations.data_source_format import hist_date_text
    from integrations.data_source_tickflow import fetch_stock_tickflow

    start_s, end_s = hist_date_text(start), hist_date_text(end)
    for source, fetch in (("tickflow", fetch_stock_tickflow), ("akshare", fetch_stock_akshare)):
        try:
            return fetch(code, start_s, end_s, "none" if source == "tickflow" else ""), source
        except Exception as exc:
            logger.warning("影子回合日线 %s %s 拉取失败: %s", source, code, exc)
    return None, ""


def run_round_trip_annotation(account_id: str = SHADOW_ACCOUNT_ID) -> dict[str, int]:
    from integrations.supabase_shadow import load_filled_trade_events, merge_event_payload

    return annotate_closed_round_trips(
        load_filled_trade_events(account_id),
        fetch_bars=fetch_unadjusted_daily,
        write_payload=lambda key, patch, current: merge_event_payload(account_id, key, patch, current),
    )


def _round_trip_record(trip: ShadowRoundTrip, fetch_bars: BarFetcher) -> dict[str, Any]:
    bars, source = fetch_bars(trip.code, trip.buy_date, trip.sell_date)
    return round_trip_payload(trip, interval_high_low(bars, trip.buy_date, trip.sell_date), source)
