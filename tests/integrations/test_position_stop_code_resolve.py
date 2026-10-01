"""止损落库必须按库内真实 code 命中，不能只 eq 规范化码。"""

from __future__ import annotations

from typing import Any

import pytest

from core.constants import TABLE_PORTFOLIO_POSITIONS, TABLE_PORTFOLIOS
from integrations import supabase_portfolio as sp


class _FakeResponse:
    def __init__(self, data: list[dict[str, Any]] | None):
        self.data = data


class _FakeTable:
    def __init__(self, name: str, store: dict[str, list[dict[str, Any]]]):
        self.name = name
        self.store = store

    def select(self, *_a, **_k):
        return _FakeQuery(self).select()

    def update(self, payload: dict[str, Any]):
        return _FakeQuery(self).update(payload)

    def _select(self, filters: dict[str, Any]) -> _FakeResponse:
        rows = list(self.store.get(self.name) or [])
        matched = [row for row in rows if all(row.get(k) == v for k, v in filters.items())]
        return _FakeResponse(matched)

    def _update(self, filters: dict[str, Any], payload: dict[str, Any]) -> _FakeResponse:
        rows = self.store.setdefault(self.name, [])
        matched: list[dict[str, Any]] = []
        for row in rows:
            if all(row.get(k) == v for k, v in filters.items()):
                row.update(payload)
                matched.append(dict(row))
        return _FakeResponse(matched)


class _FakeQuery:
    def __init__(self, table: _FakeTable):
        self.table = table
        self._filters: dict[str, Any] = {}
        self._payload: dict[str, Any] | None = None
        self._op = "select"

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def update(self, payload: dict[str, Any]):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def eq(self, column: str, value: Any, **_k):
        self._filters[column] = value
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self._op == "select":
            return self.table._select(self._filters)
        return self.table._update(self._filters, self._payload or {})


class _FakeClient:
    def __init__(self, store: dict[str, list[dict[str, Any]]]):
        self.store = store

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(name, self.store)


def _store_with_legacy_hk() -> dict[str, list[dict[str, Any]]]:
    return {
        TABLE_PORTFOLIOS: [
            {"portfolio_id": "USER_LIVE:u1", "free_cash": 10_000.0, "total_equity": 50_000.0, "updated_at": ""}
        ],
        TABLE_PORTFOLIO_POSITIONS: [
            {
                "portfolio_id": "USER_LIVE:u1",
                "code": "700.HK",
                "name": "腾讯控股",
                "shares": 100,
                "cost_price": 280.0,
                "buy_dt": "20260101",
                "stop_loss": 250.0,
                "updated_at": "",
            }
        ],
    }


def test_update_position_stops_writes_legacy_hk_db_code(monkeypatch):
    """Step4 工单码是 00700.HK；库内历史行可能是 700.HK，必须按库内码更新。"""
    store = _store_with_legacy_hk()
    client = _FakeClient(store)
    monkeypatch.setattr(sp, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(sp, "require_server_write_context", lambda *_a, **_k: None)
    monkeypatch.setattr(sp, "_get_supabase_admin_client", lambda: client)

    ok = sp.update_position_stops("USER_LIVE:u1", [{"code": "00700.HK", "stop_loss": 310.0}])

    assert ok is True
    assert store[TABLE_PORTFOLIO_POSITIONS][0]["stop_loss"] == 310.0
    assert store[TABLE_PORTFOLIO_POSITIONS][0]["code"] == "700.HK"


def test_set_position_stops_writes_legacy_hk_db_code(monkeypatch):
    store = _store_with_legacy_hk()
    client = _FakeClient(store)
    monkeypatch.setattr(sp, "_resolve_write_client", lambda _client, _op: client)

    ok, written = sp.set_position_stops("USER_LIVE:u1", [{"code": "00700.HK", "stop_loss": 305.5}], client=client)

    assert ok is True
    assert written == 1
    assert store[TABLE_PORTFOLIO_POSITIONS][0]["stop_loss"] == 305.5


def test_update_position_stops_skips_missing_new_entry_code(monkeypatch):
    """PROBE/ATTACK 新开仓尚无持仓行时不得把整批止损写成失败。"""
    store = {
        TABLE_PORTFOLIOS: [
            {"portfolio_id": "USER_LIVE:u1", "free_cash": 10_000.0, "total_equity": 10_000.0, "updated_at": ""}
        ],
        TABLE_PORTFOLIO_POSITIONS: [],
    }
    client = _FakeClient(store)
    monkeypatch.setattr(sp, "is_supabase_configured", lambda: True)
    monkeypatch.setattr(sp, "require_server_write_context", lambda *_a, **_k: None)
    monkeypatch.setattr(sp, "_get_supabase_admin_client", lambda: client)

    ok = sp.update_position_stops("USER_LIVE:u1", [{"code": "600519", "stop_loss": 1700.0}])

    assert ok is True
    assert store[TABLE_PORTFOLIO_POSITIONS] == []


@pytest.fixture(params=["admin", "user"])
def stop_writer(request, monkeypatch):
    def write(client, updates):
        if request.param == "user":
            return sp.set_position_stops("USER_LIVE:u1", updates, client=client)
        monkeypatch.setattr(sp, "is_supabase_configured", lambda: True)
        monkeypatch.setattr(sp, "require_server_write_context", lambda *_a, **_k: None)
        monkeypatch.setattr(sp, "_get_supabase_admin_client", lambda: client)
        return sp.update_position_stops("USER_LIVE:u1", updates), None

    return write


@pytest.mark.parametrize("failed_table", [TABLE_PORTFOLIOS, TABLE_PORTFOLIO_POSITIONS])
def test_stop_read_error_is_not_a_successful_empty_portfolio(monkeypatch, stop_writer, failed_table):
    store = _store_with_legacy_hk()
    original_select = _FakeTable._select
    updates = []

    def select(table, filters):
        if table.name == failed_table:
            raise RuntimeError("database read unavailable")
        return original_select(table, filters)

    monkeypatch.setattr(_FakeTable, "_select", select)
    monkeypatch.setattr(_FakeTable, "_update", lambda *args: updates.append(args))

    ok, written = stop_writer(_FakeClient(store), [{"code": "00700.HK", "stop_loss": 310.0}])

    assert ok is False
    assert written in (None, 0)
    assert updates == []
    assert store[TABLE_PORTFOLIO_POSITIONS][0]["stop_loss"] == 250.0


def test_stop_missing_portfolio_is_not_a_successful_empty_portfolio(stop_writer):
    ok, written = stop_writer(_FakeClient({}), [{"code": "00700.HK", "stop_loss": 310.0}])

    assert ok is False
    assert written in (None, 0)


@pytest.mark.parametrize("has_existing_holding", [False, True])
def test_stop_successful_read_allows_missing_new_entry(stop_writer, has_existing_holding):
    store = _store_with_legacy_hk()
    if not has_existing_holding:
        store[TABLE_PORTFOLIO_POSITIONS] = []

    ok, written = stop_writer(_FakeClient(store), [{"code": "600519", "stop_loss": 1700.0}])

    assert ok is True
    assert written in (None, 0)
    assert all(row["code"] != "600519" for row in store[TABLE_PORTFOLIO_POSITIONS])
    if has_existing_holding:
        assert store[TABLE_PORTFOLIO_POSITIONS][0]["stop_loss"] == 250.0


@pytest.mark.parametrize("db_code,want_code", [("700.HK", "00700.HK"), ("700.hk", "00700.HK"), ("aapl", "AAPL")])
@pytest.mark.parametrize("stop_loss", [310.0, None])
def test_stop_legacy_codes_update_or_clear_only_target_portfolio(stop_writer, db_code, want_code, stop_loss):
    store = _store_with_legacy_hk()
    row = store[TABLE_PORTFOLIO_POSITIONS][0]
    row["code"] = db_code
    other = dict(row, portfolio_id="USER_LIVE:u2")
    store[TABLE_PORTFOLIO_POSITIONS].append(other)

    ok, written = stop_writer(_FakeClient(store), [{"code": want_code, "stop_loss": stop_loss}])

    assert ok is True
    assert written in (None, 1)
    assert row["code"] == db_code
    assert row["stop_loss"] == stop_loss
    assert other["stop_loss"] == 250.0


@pytest.mark.parametrize("failure", ["zero_rows", "exception"])
def test_stop_matched_holding_update_failure_is_reported(monkeypatch, stop_writer, failure):
    store = _store_with_legacy_hk()
    attempts = []

    def update(table, filters, payload):
        attempts.append((filters, payload))
        if failure == "exception":
            raise RuntimeError("database write unavailable")
        return _FakeResponse([])

    monkeypatch.setattr(_FakeTable, "_update", update)

    ok, written = stop_writer(_FakeClient(store), [{"code": "00700.HK", "stop_loss": 310.0}])

    assert ok is False
    assert written in (None, 0)
    assert attempts == [({"portfolio_id": "USER_LIVE:u1", "code": "700.HK"}, {"stop_loss": 310.0})]
    assert store[TABLE_PORTFOLIO_POSITIONS][0]["stop_loss"] == 250.0


def test_cloud_stop_read_failure_does_not_update_local_mirror(monkeypatch):
    from agents import portfolio_tools

    def fail_read(*_args):
        raise RuntimeError("database read unavailable")

    local_updates = []
    monkeypatch.setattr(_FakeTable, "_select", fail_read)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx: True)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx: "USER_LIVE:u1")
    monkeypatch.setattr(portfolio_tools, "get_user_client", lambda _ctx: _FakeClient(_store_with_legacy_hk()))
    monkeypatch.setattr(portfolio_tools, "with_auth_retry", lambda _ctx, fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr("integrations.local_db.set_local_position_stop", lambda *args: local_updates.append(args))

    result = portfolio_tools.set_stop_loss(code="00700.HK", stop_loss=310.0)

    assert result == {"error": "云端止损写入失败"}
    assert local_updates == []
