import json
import logging
import os
import re
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import akshare as ak
import pandas as pd

from core.cn_boards import cn_board, is_supported_cn_board
from utils.atomic_io import atomic_write_json as _atomic_json_dump

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_TRADE_DATES_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_STOCK_LIST_CACHE_TTL_SECONDS = 24 * 60 * 60
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TradingWindow:
    start_trade_date: date
    end_trade_date: date


def _cache_path(filename: str) -> Path:
    return _DATA_DIR / filename


def _json_cache_is_fresh(path: Path, ttl_seconds: int) -> bool:
    try:
        return path.exists() and time.time() - path.stat().st_mtime <= ttl_seconds
    except Exception:
        logger.debug("cache stat failed: %s", path, exc_info=True)
        return False


def _read_trade_dates_cache(cache_path: Path) -> list[date]:
    try:
        with open(cache_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[date] = []
    for item in raw:
        try:
            out.append(pd.to_datetime(item).date())
        except Exception:
            continue
    return sorted(out)


def _write_trade_dates_cache(cache_path: Path, dates: list[date]) -> None:
    try:
        _atomic_json_dump(cache_path, [d.strftime("%Y-%m-%d") for d in dates])
    except Exception:
        logger.debug("failed to write trade dates cache", exc_info=True)


def _fetch_trade_dates_akshare() -> list[date]:
    df = ak.tool_trade_date_hist_sina()
    if df is None or df.empty:
        raise RuntimeError("akshare trade calendar empty")
    col = "trade_date"
    if col not in df.columns:
        if len(df.columns) != 1:
            raise RuntimeError("trade calendar column not found")
        col = str(df.columns[0])
    s = pd.to_datetime(df[col], errors="coerce").dropna().dt.date
    dates = sorted(set(s.tolist()))
    if not dates:
        raise RuntimeError("trade calendar parsed empty")
    dates.append(date(year=1992, month=5, day=4))
    return sorted(set(dates))


def _fetch_trade_dates_tushare() -> list[date]:
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise RuntimeError("TUSHARE_TOKEN 未配置或无效")
    end_s = (date.today() + timedelta(days=366)).strftime("%Y%m%d")
    df = pro.trade_cal(
        exchange="SSE",
        start_date="19900101",
        end_date=end_s,
        fields="cal_date,is_open",
    )
    if df is None or df.empty:
        raise RuntimeError("tushare trade_cal empty")
    open_df = df[pd.to_numeric(df["is_open"], errors="coerce") == 1]
    if open_df.empty:
        raise RuntimeError("tushare trade_cal has no open dates")
    dates = pd.to_datetime(open_df["cal_date"], errors="coerce").dropna().dt.date
    out = sorted(set(dates.tolist()))
    if not out:
        raise RuntimeError("tushare trade_cal parsed empty")
    return out


def _fetch_trade_dates_from_sources(cache_path: Path) -> tuple[list[date], Exception | None]:
    last_err: Exception | None = None
    try:
        dates = _fetch_trade_dates_tushare()
        if dates:
            _write_trade_dates_cache(cache_path, dates)
            return dates, None
    except Exception as exc:
        last_err = exc

    for _ in range(3):
        try:
            dates = _fetch_trade_dates_akshare()
            if dates:
                _write_trade_dates_cache(cache_path, dates)
                return dates, None
        except Exception as exc:
            last_err = exc
        time.sleep(0.6)
    return [], last_err


def _approx_trade_dates(last_err: Exception | None) -> list[date]:
    if os.getenv("ALLOW_APPROX_TRADE_CALENDAR", "").strip().lower() not in _TRUE_ENV_VALUES:
        raise RuntimeError(
            "failed to load accurate trade calendar and no cache available; "
            "set ALLOW_APPROX_TRADE_CALENDAR=1 if you accept business-day approximation"
        ) from last_err

    end = date.today() + timedelta(days=366)
    start = date(1990, 1, 1)
    approx = pd.bdate_range(start=start, end=end).date.tolist()
    approx.sort()
    if not approx:
        raise RuntimeError(f"failed to build trade calendar: {last_err}")
    return approx


def _trade_dates() -> list[date]:
    cache_path = _cache_path("trade_dates_cache.json")
    if _json_cache_is_fresh(cache_path, _TRADE_DATES_CACHE_TTL_SECONDS):
        cached = _read_trade_dates_cache(cache_path)
        if cached:
            return cached

    dates, last_err = _fetch_trade_dates_from_sources(cache_path)
    if dates:
        return dates

    cached = _read_trade_dates_cache(cache_path)
    if cached:
        return cached
    return _approx_trade_dates(last_err)


@lru_cache(maxsize=1)
def cached_trade_dates() -> tuple[date, ...]:
    return tuple(_trade_dates())


def resolve_trading_window(end_calendar_day: date, trading_days: int) -> TradingWindow:
    if trading_days <= 0:
        raise ValueError("trading_days must be > 0")
    dates = list(cached_trade_dates())
    idx = bisect_right(dates, end_calendar_day) - 1
    if idx < 0:
        raise RuntimeError("trade calendar has no date <= end_calendar_day")
    if idx - (trading_days - 1) < 0:
        raise RuntimeError("trade calendar does not have enough historical dates")
    start_trade = dates[idx - (trading_days - 1)]
    end_trade = dates[idx]
    return TradingWindow(start_trade_date=start_trade, end_trade_date=end_trade)


def _read_stock_list_cache(cache_path: Path) -> list[dict[str, str]]:
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"code": str(item.get("code", "")), "name": str(item.get("name", ""))}
        for item in data
        if isinstance(item, dict)
    ]


def _write_stock_list_cache(cache_path: Path, records: list[dict[str, str]], source: str) -> None:
    try:
        _atomic_json_dump(cache_path, records)
    except Exception:
        logger.debug("failed to write %s stock list cache", source, exc_info=True)


def _stock_records_from_frame(info: pd.DataFrame, code_column: str) -> list[dict[str, str]]:
    if info is None or info.empty:
        return []
    if code_column not in info.columns or "name" not in info.columns:
        return []
    frame = info.copy()
    return [
        {"code": str(row[code_column]), "name": str(row["name"])} for _, row in frame[[code_column, "name"]].iterrows()
    ]


def _fetch_stocks_tushare() -> list[dict[str, str]]:
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        return []
    info = pro.stock_basic(exchange="", list_status="L", fields="symbol,name")
    if info is None or info.empty:
        raise RuntimeError("tushare stock_basic empty")
    return _stock_records_from_frame(info, "symbol")


def _fetch_stocks_akshare() -> list[dict[str, str]]:
    info = ak.stock_info_a_code_name()
    records = _stock_records_from_frame(info, "code")
    if not records:
        raise RuntimeError("akshare stock list empty")
    return records


def get_all_stocks() -> list[dict[str, str]]:
    """
    Get all A-share stock codes and names.
    Returns:
        list of dict: [{"code": "000001", "name": "平安银行"}, ...]
    """
    cache_path = _cache_path("stock_list_cache.json")
    if _json_cache_is_fresh(cache_path, _STOCK_LIST_CACHE_TTL_SECONDS):
        cached = _read_stock_list_cache(cache_path)
        if cached:
            return cached

    try:
        records = _fetch_stocks_tushare()
        if records:
            _write_stock_list_cache(cache_path, records, "tushare")
            return records
    except Exception as exc:
        logger.warning("Tushare error fetching stock list: %s. Trying akshare...", exc)

    try:
        records = _fetch_stocks_akshare()
        _write_stock_list_cache(cache_path, records, "akshare")
        return records
    except Exception as exc:
        logger.warning("Network error fetching stock list: %s. Trying cache...", exc)
        return _read_stock_list_cache(cache_path)


def get_stocks_by_board(board_name: str = "all") -> list[dict[str, str]]:
    """
    Filter stocks by board.
    Args:
        board_name:
        - "all": 主板+创业板+科创板+北交所
        - "main_chinext": 主板+创业板+科创板（旧入口同义，不含北交所）
        - "main_chinext_star": 主板+创业板+科创板（不含北交所）
        - "main": 主板
        - "chinext": 创业板
        - "star": 科创板
        - "bse": 北交所
    """
    all_stocks = get_all_stocks()
    board = str(board_name or "all").strip().lower()
    if board in {"all", "full"}:
        return [s for s in all_stocks if is_supported_cn_board(s.get("code"))]
    if board in {"main_chinext", "main_chinext_star"}:
        return [s for s in all_stocks if is_supported_cn_board(s.get("code"), include_bse=False)]
    if board in {"main", "chinext", "star", "bse"}:
        return [s for s in all_stocks if cn_board(s.get("code")) == board]
    return []


def fetch_hist(symbol: str, window: TradingWindow, adjust: str, *, user_id: str = "") -> pd.DataFrame:
    """个股日线：tickflow 优先（qfq），失败再回退其它数据源"""
    from integrations.stock_hist_repository import get_stock_hist

    return get_stock_hist(
        symbol=symbol,
        start_date=window.start_trade_date,
        end_date=window.end_trade_date,
        adjust=adjust or "",
    )


def normalize_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        s = str(raw).strip()
        if not s:
            continue
        if not re.fullmatch(r"\d{6}", s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out
