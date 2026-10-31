"""Baostock stock-history provider."""

from __future__ import annotations

import atexit
import io
import logging
import os
import socket
import threading
import time
from contextlib import redirect_stdout, suppress

import pandas as pd

from integrations.data_source_format import SH_PREFIXES, STOCK_HIST_COLUMNS

logger = logging.getLogger(__name__)

BAOSTOCK_MAX_SECONDS = float(os.getenv("BAOSTOCK_MAX_SECONDS", "6.0"))
_SOCKET_TIMEOUT = float(os.getenv("BAOSTOCK_SOCKET_TIMEOUT", "3.0"))
_CIRCUIT_THRESHOLD = int(os.getenv("BAOSTOCK_CIRCUIT_THRESHOLD", "10"))
# 10002001/05/06/07：socket、发送失败、发送超时、接收失败。
_SESSION_LOST_CODES = frozenset({"10002001", "10002005", "10002006", "10002007"})
_LOGGED = False
_SESSION_PID: int | None = None
_EXIT_HOOKED = False
_MODULE = None
_LOCK = threading.RLock()
_CONSEC_FAILS = 0
_CIRCUIT_OPEN = False
_CIRCUIT_NOTE = ""


def baostock_circuit_state() -> tuple[bool, str]:
    with _LOCK:
        return (_CIRCUIT_OPEN, _CIRCUIT_NOTE)


def baostock_mark_success() -> None:
    global _CONSEC_FAILS
    with _LOCK:
        _CONSEC_FAILS = 0


def baostock_mark_failure(reason: str, *, debug_enabled: bool = False) -> None:
    global _CONSEC_FAILS, _CIRCUIT_OPEN, _CIRCUIT_NOTE
    with _LOCK:
        _CONSEC_FAILS += 1
        if _CIRCUIT_OPEN or _CIRCUIT_THRESHOLD <= 0 or _CONSEC_FAILS < _CIRCUIT_THRESHOLD:
            return
        _CIRCUIT_OPEN = True
        _CIRCUIT_NOTE = f"consecutive_failures={_CONSEC_FAILS}, reason={reason}"
        if debug_enabled:
            logger.debug("baostock circuit opened: %s", _CIRCUIT_NOTE)


def fetch_stock_baostock(symbol: str, start: str, end: str) -> pd.DataFrame:
    bs_code = f"sh.{symbol}" if symbol.startswith(SH_PREFIXES) else f"sz.{symbol}"
    start_dash = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_dash = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    rows, fields = query_history_k_data(
        bs_code,
        start_dash,
        end_dash,
        fields="date,open,high,low,close,volume,amount,pctChg",
        adjustflag="2",
    )
    if not rows:
        raise RuntimeError("baostock empty")
    return _baostock_frame(rows, fields)


def query_history_k_data(
    bs_code: str,
    start_dash: str,
    end_dash: str,
    *,
    fields: str,
    adjustflag: str,
) -> tuple[list[list[str]], list[str]]:
    """Process-wide baostock session: one global socket, serialized under ``_LOCK``.

    Index and stock callers must share this entrypoint. A second login/logout on
    another thread replaces ``baostock.common.context.default_socket`` and can
    splice foreign rows into an in-flight ``rs.next()`` loop.
    """
    started = time.monotonic()
    with _LOCK:
        return _query_history_rows(bs_code, start_dash, end_dash, started, fields=fields, adjustflag=adjustflag)


def _query_history_rows(
    bs_code: str,
    start_dash: str,
    end_dash: str,
    started: float,
    *,
    fields: str,
    adjustflag: str,
) -> tuple[list[list[str]], list[str]]:
    global _LOGGED
    old_sock_timeout = socket.getdefaulttimeout()
    if _SOCKET_TIMEOUT > 0:
        socket.setdefaulttimeout(_SOCKET_TIMEOUT)
    try:
        bs = _ensure_login()
        rs = bs.query_history_k_data_plus(
            bs_code,
            fields,
            start_date=start_dash,
            end_date=end_dash,
            frequency="d",
            adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            # 会话长驻后断线不会自愈：不复位的话之后每次都复用这条死 socket。
            if rs.error_code in _SESSION_LOST_CODES:
                _LOGGED = False
            raise RuntimeError(f"baostock: {rs.error_msg}")
        rows: list[list[str]] = []
        while rs.next():
            if BAOSTOCK_MAX_SECONDS > 0 and (time.monotonic() - started) > BAOSTOCK_MAX_SECONDS:
                raise TimeoutError(f"baostock hard timeout > {BAOSTOCK_MAX_SECONDS:.2f}s")
            rows.append(rs.get_row_data())
        return rows, rs.fields
    finally:
        socket.setdefaulttimeout(old_sock_timeout)


def _baostock_frame(rows: list[list[str]], fields: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=fields)
    df = df.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "amount": "成交额",
            "pctChg": "涨跌幅",
        }
    )
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    for column in ["开盘", "最高", "最低", "收盘", "成交量", "成交额", "涨跌幅"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["换手率"] = pd.NA
    df["振幅"] = pd.NA
    return df[list(STOCK_HIST_COLUMNS)].copy()


def _logout_on_exit() -> None:
    global _LOGGED
    with _LOCK:
        bs = _MODULE
        if not _LOGGED or bs is None:
            return
        # baostock 用裸 print 报 "logout success!"，绕过 logging 级别；重定向只盖这一行调用。
        with suppress(BaseException), redirect_stdout(io.StringIO()):
            bs.logout()
        _LOGGED = False


def _ensure_login():
    global _LOGGED, _SESSION_PID, _EXIT_HOOKED, _MODULE
    import baostock as bs

    _MODULE = bs
    # fork 出的子进程继承 _LOGGED，但父子不能共用同一条 socket。
    if _LOGGED and _SESSION_PID == os.getpid():
        return bs
    # 同上，baostock 登录成功也是裸 print，这里吞掉。
    with redirect_stdout(io.StringIO()):
        login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login: {login.error_msg}")
    _LOGGED = True
    _SESSION_PID = os.getpid()
    if not _EXIT_HOOKED:
        atexit.register(_logout_on_exit)
        _EXIT_HOOKED = True
    return bs
