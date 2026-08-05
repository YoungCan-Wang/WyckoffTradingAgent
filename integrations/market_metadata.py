"""Market metadata adapters: sectors, market cap, concept map, and concept heat."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from core.concept_filters import is_actionable_theme_name
from utils.atomic_io import atomic_write_json
from utils.env import env_flag

logger = logging.getLogger(__name__)

DATA_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
SECTOR_CACHE = DATA_CACHE_DIR / "sector_map_cache.json"
MARKET_CAP_CACHE = DATA_CACHE_DIR / "market_cap_cache.json"
FLOAT_SHARE_CACHE = DATA_CACHE_DIR / "float_share_cache.json"
CONCEPT_CACHE = DATA_CACHE_DIR / "concept_map_cache.json"
CONCEPT_HEAT_CACHE = DATA_CACHE_DIR / "concept_heat_cache.json"
CONCEPT_HEAT_HISTORY = DATA_CACHE_DIR / "concept_heat_history.json"
CACHE_TTL = 24 * 60 * 60
_WAN_YUAN_TO_YI = 1e-4
_WAN_SHARES_TO_SHARES = 1e4
CONCEPT_HEAT_TTL = 4 * 60 * 60
CONCEPT_REQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
CONCEPT_REQ_TIMEOUT = 30
CONCEPT_NOISE = frozenset(
    {
        "昨日涨停",
        "昨日连板",
        "注册制次新股",
        "新股与次新股",
        "科创次新股",
        "融资融券",
        "沪股通",
        "深股通",
        "北交所概念",
        "MSCI概念",
        "ST板块",
        "转债标的",
        "高管增持",
        "股权激励",
        "员工持股",
        "昨日触板",
        "创业板重组松绑",
        "送转预期",
    }
)


def fetch_sector_map() -> dict[str, str]:
    cached = read_json_cache(SECTOR_CACHE, CACHE_TTL)
    if isinstance(cached, dict):
        return cached
    pro = _tushare_pro()
    if pro is None:
        return stale_json_cache(SECTOR_CACHE, {})
    try:
        df = pro.stock_basic(fields="ts_code,industry")
    except Exception as exc:
        debug_metadata_fail("tushare_stock_basic", exc)
        return stale_json_cache(SECTOR_CACHE, {})
    if df is None or df.empty:
        return stale_json_cache(SECTOR_CACHE, {})
    mapping = {
        _ts_code_to_symbol(str(row["ts_code"])): str(row.get("industry", "")).strip()
        for _, row in df.iterrows()
        if _ts_code_to_symbol(str(row["ts_code"])) and str(row.get("industry", "")).strip()
    }
    write_json_cache(SECTOR_CACHE, mapping, "sector_cache_write")
    return mapping


def fetch_market_cap_map() -> dict[str, float]:
    cached = read_json_cache(MARKET_CAP_CACHE, CACHE_TTL)
    if isinstance(cached, dict):
        return {k: float(v) for k, v in cached.items()}
    pro = _tushare_pro()
    if pro is None:
        return _stale_float_cache(MARKET_CAP_CACHE)
    mapping = _recent_daily_basic_map(pro, "total_mv", _WAN_YUAN_TO_YI)
    if mapping:
        write_json_cache(MARKET_CAP_CACHE, mapping, "market_cap_cache_write")
    return mapping


def fetch_float_share_map() -> dict[str, float]:
    """全市场流通股本映射（单位：股）。

    行情源的日线不返回换手率，漏斗用 成交量 / 流通股本 自行折算。
    """
    cached = read_json_cache(FLOAT_SHARE_CACHE, CACHE_TTL)
    if isinstance(cached, dict):
        return {k: float(v) for k, v in cached.items()}
    pro = _tushare_pro()
    if pro is None:
        return _stale_float_cache(FLOAT_SHARE_CACHE)
    mapping = _recent_daily_basic_map(pro, "float_share", _WAN_SHARES_TO_SHARES)
    if mapping:
        write_json_cache(FLOAT_SHARE_CACHE, mapping, "float_share_cache_write")
    return mapping


def fetch_concept_map() -> dict[str, list[str]]:
    cached = read_json_cache(CONCEPT_CACHE, CACHE_TTL)
    if isinstance(cached, dict):
        return cached
    try:
        mapping = fetch_concept_map_from_eastmoney()
    except Exception as exc:
        debug_metadata_fail("concept_map_fetch", exc)
        return stale_json_cache(CONCEPT_CACHE, {})
    if not mapping:
        return stale_json_cache(CONCEPT_CACHE, {})
    write_json_cache(CONCEPT_CACHE, mapping, "concept_cache_write")
    return mapping


def fetch_concept_heat() -> list[dict[str, Any]]:
    cached = read_json_cache(CONCEPT_HEAT_CACHE, CONCEPT_HEAT_TTL)
    if isinstance(cached, list):
        return cached
    try:
        items = fetch_concept_heat_from_ths()
    except Exception as exc:
        debug_metadata_fail("concept_heat_fetch", exc)
        return stale_json_cache(CONCEPT_HEAT_CACHE, [])
    if not items:
        return stale_json_cache(CONCEPT_HEAT_CACHE, [])
    write_json_cache(CONCEPT_HEAT_CACHE, items, "concept_heat_cache_write")
    return items


def update_concept_heat_history(today: str, heat: list[dict[str, Any]], top_n: int = 20) -> None:
    history = stale_json_cache(CONCEPT_HEAT_HISTORY, {})
    top_items = _top_heat_items(heat, top_n)
    history[today] = {it["name"]: {"pct": it["pct"], "inflow": it["net_inflow"]} for it in top_items}
    sorted_dates = sorted(history.keys(), reverse=True)[:20]
    write_json_cache(CONCEPT_HEAT_HISTORY, {d: history[d] for d in sorted_dates}, "concept_heat_history_write")
    _upsert_concept_heat_history(today, heat, top_n)


def _top_heat_items(heat: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    clean = [item for item in heat if str(item.get("name", "")).strip()]
    limit = max(int(top_n), 1)
    selected: dict[str, dict[str, Any]] = {}
    for item in sorted(clean, key=lambda x: x.get("net_inflow", 0), reverse=True)[:limit]:
        selected[str(item.get("name"))] = item
    for item in sorted(clean, key=lambda x: x.get("pct", 0), reverse=True)[:limit]:
        selected[str(item.get("name"))] = item
    return list(selected.values())


def detect_theme_lines(min_days: int = 3, as_of_date: str | None = None) -> list[str]:
    history = stale_json_cache(CONCEPT_HEAT_HISTORY, {})
    if not isinstance(history, dict):
        history = {}
    if as_of_date:
        history = {d: v for d, v in history.items() if d <= as_of_date}
    if len(history) < min_days:
        return []
    sorted_dates = sorted(history.keys(), reverse=True)
    concept_streak: dict[str, int] = {}
    for concept in history.get(sorted_dates[0], {}):
        streak = _concept_streak(history, sorted_dates, concept)
        if streak >= min_days:
            concept_streak[concept] = streak
    return sorted(concept_streak, key=lambda c: concept_streak[c], reverse=True)


def fetch_concept_map_from_eastmoney() -> dict[str, list[str]]:
    import requests

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    mapping: dict[str, list[str]] = {}
    for page in range(1, 20):
        params = {
            "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
            "columns": "SECURITY_CODE,BOARD_NAME",
            "filter": '(IS_PRECISE="1")',
            "pageSize": 5000,
            "pageNumber": page,
        }
        response = requests.get(url, params=params, timeout=CONCEPT_REQ_TIMEOUT, headers=CONCEPT_REQ_HEADERS)
        rows = ((response.json().get("result") or {}).get("data")) or []
        if not rows:
            break
        _append_concept_rows(mapping, rows)
        if len(rows) < 5000:
            break
        time.sleep(0.2)
    return mapping


def fetch_concept_heat_from_ths() -> list[dict[str, Any]]:
    import requests

    response = requests.get("https://q.10jqka.com.cn/gn/", timeout=CONCEPT_REQ_TIMEOUT, headers=CONCEPT_REQ_HEADERS)
    response.encoding = "gbk"
    match = re.search(r"id=\"gnSection\"\s+value='(.*?)'", response.text, re.DOTALL)
    if not match:
        return []
    items = [_concept_heat_row(item) for item in json.loads(match.group(1)).values()]
    out = [item for item in items if item]
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out


def read_json_cache(path: Path, ttl_seconds: int) -> object | None:
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < ttl_seconds:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
    except Exception as exc:
        debug_metadata_fail(f"{path.name}_read", exc)
    return None


def stale_json_cache(path: Path, default):
    try:
        if path.exists():
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
    except Exception as exc:
        debug_metadata_fail(f"{path.name}_fallback_read", exc)
    return default


def write_json_cache(path: Path, payload: object, debug_label: str) -> None:
    try:
        atomic_write_json(path, payload)
    except Exception as exc:
        debug_metadata_fail(debug_label, exc)


def debug_metadata_fail(source: str, err: Exception) -> None:
    if env_flag("DATA_SOURCE_DEBUG"):
        logger.debug("%s failed: %s: %s", source, type(err).__name__, err)


def _recent_daily_basic_map(pro, field: str, scale: float) -> dict[str, float]:
    for offset in range(5):
        trade_date = (date.today() - timedelta(days=1 + offset)).strftime("%Y%m%d")
        try:
            df = pro.daily_basic(trade_date=trade_date, fields=f"ts_code,{field}")
        except Exception as exc:
            debug_metadata_fail(f"tushare_daily_basic[{trade_date}]", exc)
            continue
        if df is not None and not df.empty:
            return _daily_basic_field_map(df, field, scale)
    return {}


def _daily_basic_field_map(df: pd.DataFrame, field: str, scale: float) -> dict[str, float]:
    mapping: dict[str, float] = {}
    for _, row in df.iterrows():
        sym = _ts_code_to_symbol(str(row["ts_code"]))
        value = row.get(field)
        if sym and pd.notna(value):
            mapping[sym] = float(value) * scale
    return mapping


def _append_concept_rows(mapping: dict[str, list[str]], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        name = str(row.get("BOARD_NAME", "") or "").strip()
        code = str(row.get("SECURITY_CODE", "") or "").strip()
        if code and _actionable_concept_name(name):
            mapping.setdefault(code, []).append(name)


def _concept_heat_row(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("platename", "") or "").strip()
    if not _actionable_concept_name(name):
        return None
    return {
        "name": name,
        "pct": float(item.get("199112", 0)),
        "net_inflow": float(item.get("zjjlr", 0)),
        "cid": str(item.get("cid", "")),
    }


def _concept_streak(history: dict, sorted_dates: list[str], concept: str) -> int:
    streak = 1
    for day in sorted_dates[1:]:
        if concept in history.get(day, {}):
            streak += 1
        else:
            break
    return streak


def _upsert_concept_heat_history(today: str, heat: list[dict[str, Any]], top_n: int) -> None:
    try:
        from integrations.supabase_concept_heat import upsert_concept_heat_history

        upsert_concept_heat_history(today, heat, top_n=top_n)
    except Exception as exc:
        debug_metadata_fail("concept_heat_history_supabase_write", exc)


def _stale_float_cache(path: Path) -> dict[str, float]:
    raw = stale_json_cache(path, {})
    return {k: float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _tushare_pro():
    from integrations.tushare_client import get_pro

    return get_pro()


def _ts_code_to_symbol(ts_code: str) -> str:
    return ts_code.split(".")[0] if "." in ts_code else ts_code


def _actionable_concept_name(name: str) -> bool:
    return bool(name) and name not in CONCEPT_NOISE and is_actionable_theme_name(name)


def fetch_historical_market_cap_map(as_of_date: str) -> dict[str, float]:
    """获取指定历史交易日期的全市场市值映射（单位：亿元）。"""
    trade_date = as_of_date.replace("-", "")
    pro = _tushare_pro()
    if pro is None:
        return {}
    attempts = max(int(os.getenv("MARKET_METADATA_MAX_RETRIES", "3")), 1)
    backoff = max(float(os.getenv("MARKET_METADATA_RETRY_BACKOFF_SECONDS", "1")), 0.0)
    for attempt in range(1, attempts + 1):
        try:
            df = pro.daily_basic(trade_date=trade_date, fields="ts_code,total_mv")
            if df is not None and not df.empty:
                return _daily_basic_field_map(df, "total_mv", _WAN_YUAN_TO_YI)
        except Exception as exc:
            debug_metadata_fail(f"tushare_historical_daily_basic[{trade_date}]", exc)
        if attempt < attempts and backoff > 0:
            time.sleep(backoff * attempt)
    return {}
