from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import integrations.data_source_baostock as provider
from integrations import index_data_source
from integrations.data_source_format import STOCK_HIST_COLUMNS


class _FakeResult:
    fields = ["date", "open", "high", "low", "close", "volume", "amount", "pctChg"]
    error_code = "0"
    error_msg = ""

    def __init__(self) -> None:
        self._rows = [["2026-06-01", "10", "10.5", "9.8", "10.2", "1000", "10200", "2.0"]]
        self._idx = -1

    def next(self) -> bool:
        self._idx += 1
        return self._idx < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._idx]


def _reset_provider_state(monkeypatch) -> None:
    monkeypatch.setattr(provider, "_LOGGED", False)
    monkeypatch.setattr(provider, "_SESSION_PID", None)
    monkeypatch.setattr(provider, "_EXIT_HOOKED", False)
    monkeypatch.setattr(provider, "_MODULE", None)
    monkeypatch.setattr(provider, "_CONSEC_FAILS", 0)
    monkeypatch.setattr(provider, "_CIRCUIT_OPEN", False)
    monkeypatch.setattr(provider, "_CIRCUIT_NOTE", "")


def test_fetch_stock_baostock_normalizes_rows_and_uses_exchange_prefix(monkeypatch) -> None:
    _reset_provider_state(monkeypatch)
    captured: dict[str, object] = {}

    def fake_query(code: str, fields: str, **kwargs):
        captured.update({"code": code, "fields": fields, **kwargs})
        return _FakeResult()

    fake_module = SimpleNamespace(
        login=lambda: SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
        query_history_k_data_plus=fake_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_module)
    monkeypatch.setattr(provider.atexit, "register", lambda *_args, **_kwargs: None)

    out = provider.fetch_stock_baostock("600519", "20260601", "20260602")

    assert tuple(out.columns) == STOCK_HIST_COLUMNS
    assert captured["code"] == "sh.600519"
    assert captured["start_date"] == "2026-06-01"
    assert captured["end_date"] == "2026-06-02"
    assert out.iloc[0]["日期"] == "2026-06-01"
    assert float(out.iloc[0]["收盘"]) == 10.2
    assert pd.isna(out.iloc[0]["换手率"])


def test_baostock_circuit_opens_after_configured_failures(monkeypatch) -> None:
    _reset_provider_state(monkeypatch)
    monkeypatch.setattr(provider, "_CIRCUIT_THRESHOLD", 2)

    provider.baostock_mark_failure("first")
    assert provider.baostock_circuit_state() == (False, "")

    provider.baostock_mark_failure("second")
    opened, note = provider.baostock_circuit_state()
    assert opened is True
    assert "consecutive_failures=2" in note


def test_index_baostock_reuses_shared_session_and_never_logs_out(monkeypatch) -> None:
    """Index must not call bs.logout(): that replaces the process-global socket
    while stock hist may still be mid rs.next() on the shared Cloud service."""
    _reset_provider_state(monkeypatch)
    logouts: list[str] = []
    captured: dict[str, object] = {}

    class _IndexResult(_FakeResult):
        fields = ["date", "open", "high", "low", "close", "volume", "pctChg"]

        def __init__(self) -> None:
            self._rows = [["2026-06-01", "10", "10.5", "9.8", "10.2", "1000", "1.0"]]
            self._idx = -1

    def fake_query(code: str, fields: str, **kwargs):
        captured.update({"code": code, "fields": fields, **kwargs})
        return _IndexResult()

    fake_module = SimpleNamespace(
        login=lambda: SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: logouts.append("logout"),
        query_history_k_data_plus=fake_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_module)
    monkeypatch.setattr(provider.atexit, "register", lambda *_args, **_kwargs: None)

    out = index_data_source.fetch_index_baostock("000001", "20260601", "20260602")

    assert logouts == []
    assert provider._LOGGED is True
    assert captured["code"] == "sh.000001"
    assert captured["adjustflag"] == "3"
    assert out.iloc[0]["close"] == 10.2


def test_index_and_stock_baostock_serialize_on_shared_lock(monkeypatch) -> None:
    _reset_provider_state(monkeypatch)
    order: list[str] = []
    hold = threading.Event()
    release = threading.Event()

    class _SlowStockResult(_FakeResult):
        def next(self) -> bool:
            if self._idx < 0:
                order.append("stock-start")
                hold.set()
                assert release.wait(timeout=2.0)
            return super().next()

    class _IndexResult(_FakeResult):
        fields = ["date", "open", "high", "low", "close", "volume", "pctChg"]

        def __init__(self) -> None:
            self._rows = [["2026-06-01", "10", "10.5", "9.8", "10.2", "1000", "1.0"]]
            self._idx = -1

        def next(self) -> bool:
            if self._idx < 0:
                order.append("index-start")
            return super().next()

    def fake_query(code: str, fields: str, **kwargs):
        if code.startswith("sh.600"):
            return _SlowStockResult()
        return _IndexResult()

    fake_module = SimpleNamespace(
        login=lambda: SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: order.append("logout"),
        query_history_k_data_plus=fake_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_module)
    monkeypatch.setattr(provider.atexit, "register", lambda *_args, **_kwargs: None)

    errors: list[BaseException] = []

    def run_stock() -> None:
        try:
            provider.fetch_stock_baostock("600519", "20260601", "20260602")
        except Exception as exc:
            errors.append(exc)

    stock_thread = threading.Thread(target=run_stock)
    stock_thread.start()
    assert hold.wait(timeout=2.0)
    # Index would previously login/logout without the stock lock; now it must wait.
    index_thread = threading.Thread(
        target=lambda: index_data_source.fetch_index_baostock("000001", "20260601", "20260602")
    )
    index_thread.start()
    time.sleep(0.05)
    assert "index-start" not in order
    release.set()
    stock_thread.join(timeout=2.0)
    index_thread.join(timeout=2.0)
    assert errors == []
    assert order == ["stock-start", "index-start"]
    assert "logout" not in order


def _counting_module(logins: list[str], query) -> SimpleNamespace:
    def login():
        logins.append("login")
        return SimpleNamespace(error_code="0", error_msg="")

    return SimpleNamespace(login=login, logout=lambda: None, query_history_k_data_plus=query)


def test_dropped_socket_relogs_in_on_next_query(monkeypatch) -> None:
    _reset_provider_state(monkeypatch)
    logins: list[str] = []
    results = [SimpleNamespace(error_code="10002007", error_msg="recv failed"), _FakeResult()]
    fake_module = _counting_module(logins, lambda *_args, **_kwargs: results.pop(0))
    monkeypatch.setitem(sys.modules, "baostock", fake_module)
    monkeypatch.setattr(provider.atexit, "register", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="recv failed"):
        provider.fetch_stock_baostock("600519", "20260601", "20260602")
    out = provider.fetch_stock_baostock("600519", "20260601", "20260602")

    assert logins == ["login", "login"]
    assert float(out.iloc[0]["收盘"]) == 10.2


def test_query_error_on_live_session_keeps_session(monkeypatch) -> None:
    _reset_provider_state(monkeypatch)
    logins: list[str] = []
    results = [SimpleNamespace(error_code="10004020", error_msg="bad code"), _FakeResult()]
    fake_module = _counting_module(logins, lambda *_args, **_kwargs: results.pop(0))
    monkeypatch.setitem(sys.modules, "baostock", fake_module)
    monkeypatch.setattr(provider.atexit, "register", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="bad code"):
        provider.fetch_stock_baostock("600519", "20260601", "20260602")
    provider.fetch_stock_baostock("600519", "20260601", "20260602")

    assert logins == ["login"]


def test_forked_child_opens_its_own_session(monkeypatch) -> None:
    _reset_provider_state(monkeypatch)
    logins: list[str] = []
    fake_module = _counting_module(logins, lambda *_args, **_kwargs: _FakeResult())
    monkeypatch.setitem(sys.modules, "baostock", fake_module)
    monkeypatch.setattr(provider.atexit, "register", lambda *_args, **_kwargs: None)

    provider.fetch_stock_baostock("600519", "20260601", "20260602")
    provider.fetch_stock_baostock("600519", "20260601", "20260602")
    assert logins == ["login"]

    parent_pid = provider.os.getpid()
    monkeypatch.setattr(provider.os, "getpid", lambda: parent_pid + 1)
    provider.fetch_stock_baostock("600519", "20260601", "20260602")

    assert logins == ["login", "login"]
