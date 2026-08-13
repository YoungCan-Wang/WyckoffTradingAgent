"""Index daily data source: tushare first, akshare fallback."""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import pandas as pd

from integrations.tickflow_notice import TICKFLOW_UPGRADE_URL
from utils.env import env_flag

logger = logging.getLogger(__name__)


def fetch_index_hist(code: str, start: str | date, end: str | date) -> pd.DataFrame:
    start_s = start.strftime("%Y%m%d") if isinstance(start, date) else str(start).replace("-", "")
    end_s = end.strftime("%Y%m%d") if isinstance(end, date) else str(end).replace("-", "")
    attempts = max(int(os.getenv("INDEX_DATA_MAX_RETRIES", "3")), 1)
    backoff = max(float(os.getenv("INDEX_DATA_RETRY_BACKOFF_SECONDS", "1")), 0.0)
    for attempt in range(1, attempts + 1):
        result = _fetch_index_hist_once(code, start_s, end_s)
        if result is not None:
            return result
        if attempt < attempts and backoff > 0:
            time.sleep(backoff * attempt)
    raise RuntimeError(f"大盘指数 {code} 拉取全部失败（tushare + akshare）。{_index_failure_suffix()}")


def _fetch_index_hist_once(code: str, start_s: str, end_s: str) -> pd.DataFrame | None:
    try:
        return _fetch_index_tushare(code, start_s, end_s)
    except Exception as exc:
        _debug_index_fail("tushare(index)", exc)
    try:
        return fetch_index_akshare(code, start_s, end_s)
    except Exception as exc:
        _debug_index_fail("akshare(index)", exc)
    try:
        return fetch_index_baostock(code, start_s, end_s)
    except Exception as exc:
        _debug_index_fail("baostock(index)", exc)
    return None


def fetch_index_akshare(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak

    df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError("akshare 大盘指数返回空数据")
    df = df.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "涨跌幅": "pct_chg",
        }
    )
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "pct_chg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["date", "open", "high", "low", "close", "volume", "pct_chg"]].sort_values("date").reset_index(drop=True)


def fetch_index_baostock(code: str, start: str, end: str) -> pd.DataFrame:
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock index login: {login.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            _index_to_baostock_code(code),
            "date,open,high,low,close,volume,pctChg",
            start_date=_dash_date(start),
            end_date=_dash_date(end),
            frequency="d",
            adjustflag="3",
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock index: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    if not rows:
        raise RuntimeError("baostock index empty")
    frame = pd.DataFrame(rows, columns=rs.fields).rename(columns={"pctChg": "pct_chg"})
    for column in ("open", "high", "low", "close", "volume", "pct_chg"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame[["date", "open", "high", "low", "close", "volume", "pct_chg"]].sort_values("date").reset_index(drop=True)
    )


def _fetch_index_tushare(code: str, start: str, end: str) -> pd.DataFrame:
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise RuntimeError("tushare token 未配置，跳过 tushare 大盘指数")
    df = pro.index_daily(ts_code=_index_to_ts_code(code), start_date=start, end_date=end)
    if df is None or df.empty:
        raise RuntimeError("拉取失败（非程序错误）：tushare 大盘指数返回空数据")
    df = df.copy()
    df["date"] = (
        df["trade_date"].astype(str).str[:4]
        + "-"
        + df["trade_date"].astype(str).str[4:6]
        + "-"
        + df["trade_date"].astype(str).str[6:8]
    )
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    return df[["date", "open", "high", "low", "close", "volume", "pct_chg"]].sort_values("date").reset_index(drop=True)


def _index_to_ts_code(code: str) -> str:
    text = str(code).strip()
    if "." in text:
        return text
    if text.startswith(("000", "880", "899")):
        return f"{text}.SH"
    return f"{text}.SZ"


def _index_to_baostock_code(code: str) -> str:
    text = str(code).split(".")[0].strip()
    return f"sh.{text}" if text.startswith(("000", "880", "899")) else f"sz.{text}"


def _dash_date(value: str) -> str:
    text = str(value).replace("-", "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _index_failure_suffix() -> str:
    has_tickflow = bool(os.getenv("TICKFLOW_API_KEY", "").strip())
    from integrations.tushare_client import has_tushare_token

    has_tushare = has_tushare_token()
    if not has_tickflow and not has_tushare:
        return f"请配置数据源：{TICKFLOW_UPGRADE_URL}"
    if has_tushare and not has_tickflow:
        return f"Tushare 权限不足，可购买 TickFlow 获取更稳定数据：{TICKFLOW_UPGRADE_URL}"
    return "请检查网络连通性。"


def _debug_index_fail(source: str, err: Exception) -> None:
    if env_flag("DATA_SOURCE_DEBUG"):
        logger.debug("%s failed: %s: %s", source, type(err).__name__, err)
