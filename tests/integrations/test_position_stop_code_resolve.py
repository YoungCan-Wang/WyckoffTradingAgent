"""止损落库必须按库内真实 code 命中，不能只 eq 规范化码。"""

from __future__ import annotations

from typing import Any

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
