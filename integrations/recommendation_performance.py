"""Post-entry performance refresh for global recommendation tracking tables."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from core.constants import (
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_RECOMMENDATION_TRACKING_HK,
    TABLE_RECOMMENDATION_TRACKING_US,
)
from integrations.recommendation_tracking_common import (
    chunked,
    fetch_records_from_table,
    first_recommend_date_yyyymmdd,
    ohlc_map_from_tickflow_hist,
    pick_close_on_or_before,
    recommend_date_to_yyyymmdd,
    safe_float,
    upsert_to_table,
)
from integrations.supabase_base import create_admin_client, is_admin_configured, require_server_write_context

TRACKING_TABLE_BY_MARKET = {
    "cn": TABLE_RECOMMENDATION_TRACKING,
    "hk": TABLE_RECOMMENDATION_TRACKING_HK,
    "us": TABLE_RECOMMENDATION_TRACKING_US,
}


def refresh_us_tracking_performance(max_dates: int = 60, kline_count: int = 160) -> dict[str, Any]:
    return refresh_tracking_performance("us", max_dates=max_dates, kline_count=kline_count)


def refresh_tracking_performance(
    market: str,
    *,
    max_dates: int = 60,
    kline_count: int = 160,
) -> dict[str, Any]:
    if not is_admin_configured():
        raise ValueError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY 未配置")
    market_key = resolve_tracking_market(market)
    require_server_write_context(f"refresh {market_key} tracking performance")
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TICKFLOW_API_KEY 未配置")

    client = create_admin_client()
    table = TRACKING_TABLE_BY_MARKET[market_key]
    all_records = fetch_records_from_table(client, table, "id,code,recommend_date,initial_price")
    # max_dates 只限制「刷新哪些行」；首次推荐日必须用全量历史，否则会把 sticky 锚点算成窗口内最早日。
    first_dates = first_recommend_dates_by_market_code(all_records, market_key)
    records = latest_market_records(all_records, max_dates)
    if not records:
        return empty_performance_summary()

    grouped = group_records_by_market_code(records, market_key)
    symbol_map = _tickflow_symbol_map(sorted(grouped), market_key)
    hist_map = _fetch_histories(api_key, sorted(set(symbol_map.values())), kline_count)
    hist_by_code = {code: hist_map.get(symbol) for code, symbol in symbol_map.items()}
    now_iso = datetime.now(UTC).isoformat()
    updates, codes_no_data, latest_td = build_market_performance_updates(
        grouped,
        hist_by_code,
        now_iso,
        market_key,
        first_dates=first_dates,
    )
    written = upsert_to_table(client, table, updates, optional_columns=("stop_loss_sim_pct",))
    return performance_summary(records, grouped, written, codes_no_data, latest_td, updates)


def resolve_tracking_market(market: str) -> str:
    key = str(market or "cn").strip().lower()
    if key in {"a", "a_share", "ashare"}:
        key = "cn"
    if key not in TRACKING_TABLE_BY_MARKET:
        raise ValueError(f"unsupported market: {market}, must be cn, hk, or us")
    return key


def latest_market_records(records: list[dict[str, Any]], max_dates: int) -> list[dict[str, Any]]:
    limit = max(int(max_dates), 1)
    dates = sorted(
        {day for day in (recommend_date_to_yyyymmdd(row.get("recommend_date")) for row in records) if day},
        reverse=True,
    )[:limit]
    allowed = set(dates)
    return [row for row in records if recommend_date_to_yyyymmdd(row.get("recommend_date")) in allowed]


def build_us_performance_updates(
    grouped: dict[str, list[dict[str, Any]]],
    hist_map: dict[str, pd.DataFrame],
    now_iso: str,
    *,
    first_dates: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    return build_market_performance_updates(grouped, hist_map, now_iso, "us", first_dates=first_dates)


def first_recommend_dates_by_market_code(records: list[dict[str, Any]], market: str) -> dict[str, str]:
    grouped = group_records_by_market_code(records, market)
    return {code: first_recommend_date_yyyymmdd(rows) for code, rows in grouped.items() if rows}


def build_market_performance_updates(
    grouped: dict[str, list[dict[str, Any]]],
    hist_map: dict[str, pd.DataFrame | None],
    now_iso: str,
    market: str,
    *,
    first_dates: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    market_key = resolve_tracking_market(market)
    updates: list[dict[str, Any]] = []
    codes_no_data = 0
    latest_td = ""
    for code, rows in grouped.items():
        ohlc = ohlc_map_from_tickflow_hist(hist_map.get(code))
        trade_dates = sorted(ohlc)
        if not trade_dates:
            codes_no_data += 1
            continue
        latest_td = max(latest_td, trade_dates[-1])
        first_date = (first_dates or {}).get(code) or first_recommend_date_yyyymmdd(rows)
        updates.extend(
            row
            for row in (_build_performance_update(row, code, ohlc, now_iso, market_key, first_date) for row in rows)
            if row
        )
    return updates, codes_no_data, latest_td


def empty_us_performance_summary() -> dict[str, Any]:
    return empty_performance_summary()


def empty_performance_summary() -> dict[str, Any]:
    return {
        "rows_total": 0,
        "rows_updated": 0,
        "rows_skipped": 0,
        "codes_total": 0,
        "codes_no_data": 0,
        "latest_trade_date": "",
        "mfe_ge_5": 0,
        "mfe_ge_10": 0,
        "mae_le_neg5": 0,
    }


def us_performance_summary(
    records: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
    written: int,
    codes_no_data: int,
    latest_trade_date: str,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    return performance_summary(records, grouped, written, codes_no_data, latest_trade_date, updates)


def performance_summary(
    records: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
    written: int,
    codes_no_data: int,
    latest_trade_date: str,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = empty_performance_summary()
    summary.update(
        {
            "rows_total": len(records),
            "rows_updated": written,
            "rows_skipped": max(len(records) - written, 0),
            "codes_total": len(grouped),
            "codes_no_data": codes_no_data,
            "latest_trade_date": latest_trade_date,
            "mfe_ge_5": sum(safe_float(row.get("mfe_pct")) >= 5.0 for row in updates),
            "mfe_ge_10": sum(safe_float(row.get("mfe_pct")) >= 10.0 for row in updates),
            "mae_le_neg5": sum(safe_float(row.get("mae_pct")) <= -5.0 for row in updates),
        }
    )
    return summary


def group_records_by_market_code(records: list[dict[str, Any]], market: str) -> dict[str, list[dict[str, Any]]]:
    market_key = resolve_tracking_market(market)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        code = _market_code_key(row.get("code"), market_key)
        if code:
            grouped.setdefault(code, []).append(row)
    return grouped


def _build_performance_update(
    row: dict[str, Any],
    code: str,
    ohlc: dict[str, dict[str, float]],
    now_iso: str,
    market: str,
    first_recommend_date: str = "",
) -> dict[str, Any] | None:
    trade_dates = sorted(ohlc)
    recommend_date = recommend_date_to_yyyymmdd(row.get("recommend_date"))
    event_date = pick_close_on_or_before(trade_dates, recommend_date)
    if not event_date:
        return None
    event_entry = safe_float(ohlc.get(event_date, {}).get("close"), 0.0)
    sticky_date = pick_close_on_or_before(trade_dates, first_recommend_date or recommend_date)
    sticky_entry = safe_float(ohlc.get(sticky_date, {}).get("close"), 0.0) if sticky_date else 0.0
    if sticky_entry <= 0:
        sticky_entry = safe_float(row.get("initial_price"), 0.0)
    if event_entry <= 0:
        event_entry = sticky_entry
    if sticky_entry <= 0 or event_entry <= 0:
        return None
    code_value = int(code) if market == "cn" and code.isdigit() else code
    return _performance_row(
        row,
        code_value,
        recommend_date,
        sticky_entry,
        event_entry,
        _window_rows(trade_dates, event_date, ohlc),
        now_iso,
    )


# 固定的复盘标尺，**不等于**生产止损：Step4 的 STEP4_BUY_HARD_STOP_PCT 是 -12% 灾难地板。
# 两者回答不同问题，此处刻意不跟随环境变量，否则改一次参数就会让整列历史值失去可比性。
# 阈值本身也不是调优结果——实盘样本上收益对止损松紧单调（越紧越好，一路到 -4%），
# 说明信号本身是负漂移，没有可识别的最优点，取整到 -9% 只是一把固定尺子。
STOP_LOSS_SIM_PCT = -9.0


def _performance_row(
    row: dict[str, Any],
    code: int | str,
    recommend_date: str,
    sticky_entry: float,
    event_entry: float,
    window: list[tuple[str, dict[str, float]]],
    now_iso: str,
) -> dict[str, Any] | None:
    if sticky_entry <= 0 or event_entry <= 0 or not window:
        return None
    high_date, high_row = max(window, key=lambda item: item[1]["high"])
    low_date, low_row = min(window, key=lambda item: item[1]["low"])
    latest_date, latest_row = window[-1]
    mfe_price = float(high_row["high"])
    mae_price = float(low_row["low"])
    current_price = float(latest_row["close"])
    return {
        "id": row.get("id"),
        "code": code,
        "recommend_date": int(recommend_date) if recommend_date.isdigit() else None,
        "initial_price": round(sticky_entry, 4),
        "current_price": round(current_price, 4),
        "change_pct": round((current_price / sticky_entry - 1.0) * 100.0, 2),
        "mfe_pct": round((mfe_price / event_entry - 1.0) * 100.0, 2),
        "mae_pct": round((mae_price / event_entry - 1.0) * 100.0, 2),
        "range_amp_pct": round((mfe_price / mae_price - 1.0) * 100.0, 2) if mae_price > 0 else 0.0,
        "mfe_price": round(mfe_price, 4),
        "mae_price": round(mae_price, 4),
        "mfe_date": int(high_date),
        "mae_date": int(low_date),
        "stop_loss_sim_pct": round(_simulate_stop_loss_pct(event_entry, window), 2),
        "performance_days": len(window),
        "performance_updated_at": now_iso,
        "updated_at": now_iso,
    }


def _simulate_stop_loss_pct(entry: float, window: list[tuple[str, dict[str, float]]]) -> float:
    """按固定百分比硬止损纪律回放收益，量化"没有止损"与"有止损"的实际差距。

    一旦某日最低价触及止损线视为当日止损离场（按止损价成交，不考虑跳空更差成交价）；
    否则持有到观察窗口最后一日按收盘价计算。用于复盘裸信号池的真实可交易性，不代表
    实盘已执行止损（港股/美股漏斗目前没有 Step4 OMS 执行层）。
    """
    stop_price = entry * (1.0 + STOP_LOSS_SIM_PCT / 100.0)
    for _day, ohlc in window:
        if float(ohlc["low"]) <= stop_price:
            return STOP_LOSS_SIM_PCT
    final_close = float(window[-1][1]["close"])
    return (final_close / entry - 1.0) * 100.0


def _window_rows(
    trade_dates: list[str],
    entry_date: str,
    ohlc: dict[str, dict[str, float]],
) -> list[tuple[str, dict[str, float]]]:
    return [(day, ohlc[day]) for day in trade_dates if day >= entry_date]


def _market_code_key(raw_code: Any, market: str) -> str:
    raw = str(raw_code or "").strip()
    if market == "cn":
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits[-6:].zfill(6) if digits else ""
    return raw


def _tickflow_symbol_map(codes: list[str], market: str) -> dict[str, str]:
    if market != "cn":
        return {code: code for code in codes if code}
    from integrations.tickflow_client import normalize_cn_symbol

    return {code: symbol for code in codes if (symbol := normalize_cn_symbol(code))}


def _fetch_histories(api_key: str, symbols: list[str], kline_count: int) -> dict[str, pd.DataFrame]:
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=api_key)
    hist_map: dict[str, pd.DataFrame] = {}
    for batch in chunked(symbols, _performance_batch_size()):
        hist_map.update(client.get_klines_batch(batch, period="1d", count=max(int(kline_count), 1), adjust="forward"))
    return hist_map


def _performance_batch_size() -> int:
    raw = os.getenv("RECOMMENDATION_PERFORMANCE_BATCH_SIZE", "").strip()
    try:
        return max(min(int(float(raw or 80)), 100), 1)
    except (TypeError, ValueError):
        return 80
