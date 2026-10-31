"""record_fill must seed portfolio_positions.stop_loss from the APPROVED entry ticket."""

from __future__ import annotations

from types import SimpleNamespace

from core.trade_fill import Fill
from integrations import supabase_portfolio as portfolio_store
from integrations.supabase_portfolio import (
    EquityRefreshResult,
    _approved_entry_stop_loss,
    _position_stop_missing,
    record_fill,
)


class _FakeQuery:
    def __init__(self, client: _FakeClient, table: str):
        self.client = client
        self.table = table
        self.action = ""
        self.payload = None
        self.filters: list[tuple[str, object]] = []

    def select(self, *_args, **_kwargs):
        self.action = "select"
        return self

    def insert(self, payload, **_kwargs):
        self.action = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, column: str, value):
        self.filters.append((column, value))
        return self

    def in_(self, column: str, values):
        self.filters.append((column, list(values)))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, _value: int):
        return self

    def execute(self):
        self.client.calls.append(
            {
                "table": self.table,
                "action": self.action,
                "payload": self.payload,
                "filters": list(self.filters),
            }
        )
        if self.action == "select" and self.table == "trade_orders":
            return SimpleNamespace(data=list(self.client.trade_orders))
        if self.action == "select":
            return SimpleNamespace(data=list(self.client.position_rows))
        return SimpleNamespace(data=[self.payload])


class _FakeClient:
    def __init__(self, *, trade_orders: list[dict] | None = None, position_rows: list[dict] | None = None):
        self.calls: list[dict] = []
        self.trade_orders = list(trade_orders or [])
        self.position_rows = list(position_rows or [])

    def table(self, name: str):
        return _FakeQuery(self, name)


def test_position_stop_missing_treats_blank_and_nonpositive_as_empty():
    assert _position_stop_missing(None) is True
    assert _position_stop_missing({}) is True
    assert _position_stop_missing({"stop_loss": None}) is True
    assert _position_stop_missing({"stop_loss": 0}) is True
    assert _position_stop_missing({"stop_loss": 9.4}) is False


def test_approved_entry_stop_prefers_same_day_then_recent_window():
    client = _FakeClient(
        trade_orders=[
            {
                "code": "600000",
                "action": "PROBE",
                "status": "APPROVED",
                "stop_loss": 9.4,
                "trade_date": "2026-05-14",
                "created_at": "2026-05-14T15:00:00+00:00",
            },
            {
                "code": "600000",
                "action": "ATTACK",
                "status": "APPROVED",
                "stop_loss": 9.1,
                "trade_date": "2026-05-10",
                "created_at": "2026-05-10T15:00:00+00:00",
            },
        ]
    )
    # Fill next morning: prefer T ticket over older one inside the 5-day window.
    assert _approved_entry_stop_loss(client, "USER_LIVE:u1", "600000", "20260515") == 9.4
    assert _approved_entry_stop_loss(client, "USER_LIVE:u1", "600000", "2026-05-14") == 9.4


def test_record_fill_new_buy_seeds_stop_from_approved_ticket(monkeypatch):
    client = _FakeClient(
        trade_orders=[
            {
                "code": "600000",
                "action": "PROBE",
                "status": "APPROVED",
                "stop_loss": 9.4,
                "trade_date": "2026-05-15",
                "created_at": "2026-05-15T15:00:00+00:00",
            }
        ]
    )
    monkeypatch.setattr(portfolio_store, "_resolve_write_client", lambda _client, _action: client)
    monkeypatch.setattr(
        portfolio_store,
        "load_portfolio_state",
        lambda *_a, **_k: {"free_cash": 50_000.0, "positions": []},
    )
    monkeypatch.setattr(portfolio_store, "_ensure_portfolio_exists", lambda *_a, **_k: None)
    monkeypatch.setattr(
        portfolio_store,
        "refresh_portfolio_total_equity",
        lambda *_a, **_k: EquityRefreshResult(True, 1, "ok"),
    )

    result = record_fill(
        "USER_LIVE:u1",
        Fill("600000", "buy", 100, 10.0, "2026-05-15", name="浦发银行"),
        client=client,
    )

    assert result.ok is True
    inserts = [call for call in client.calls if call["table"] == "portfolio_positions" and call["action"] == "insert"]
    assert inserts
    assert inserts[0]["payload"]["stop_loss"] == 9.4


def test_record_fill_does_not_overwrite_existing_tighter_stop(monkeypatch):
    client = _FakeClient(
        trade_orders=[
            {
                "code": "600000",
                "action": "PROBE",
                "status": "APPROVED",
                "stop_loss": 9.0,
                "trade_date": "2026-05-15",
                "created_at": "2026-05-15T15:00:00+00:00",
            }
        ],
        position_rows=[
            {
                "code": "600000",
                "name": "浦发银行",
                "shares": 1000,
                "cost_price": 10.0,
                "buy_dt": "2026-05-01",
                "stop_loss": 9.5,
            }
        ],
    )
    monkeypatch.setattr(portfolio_store, "_resolve_write_client", lambda _client, _action: client)
    monkeypatch.setattr(
        portfolio_store,
        "load_portfolio_state",
        lambda *_a, **_k: {
            "free_cash": 50_000.0,
            "positions": [
                {
                    "code": "600000",
                    "name": "浦发银行",
                    "shares": 1000,
                    "cost": 10.0,
                    "buy_dt": "2026-05-01",
                    "stop_loss": 9.5,
                }
            ],
        },
    )
    monkeypatch.setattr(
        portfolio_store,
        "refresh_portfolio_total_equity",
        lambda *_a, **_k: EquityRefreshResult(True, 1, "ok"),
    )

    result = record_fill(
        "USER_LIVE:u1",
        Fill("600000", "buy", 100, 10.0, "2026-05-15", name="浦发银行"),
        client=client,
    )

    assert result.ok is True
    updates = [call for call in client.calls if call["table"] == "portfolio_positions" and call["action"] == "update"]
    assert updates
    assert "stop_loss" not in updates[0]["payload"]


def _entry_ticket(stop_loss: float, trade_date: str) -> dict:
    return {
        "code": "600000",
        "action": "PROBE",
        "status": "APPROVED",
        "stop_loss": stop_loss,
        "trade_date": trade_date,
        "created_at": f"{trade_date}T15:00:00+00:00",
    }


def test_approved_entry_stop_ignores_ticket_dated_after_fill():
    client = _FakeClient(trade_orders=[_entry_ticket(9.4, "2026-05-16")])

    assert _approved_entry_stop_loss(client, "USER_LIVE:u1", "600000", "2026-05-15") is None


def _record_new_buy(monkeypatch, client: _FakeClient, price: float) -> list[dict]:
    monkeypatch.setattr(portfolio_store, "_resolve_write_client", lambda _client, _action: client)
    monkeypatch.setattr(
        portfolio_store,
        "load_portfolio_state",
        lambda *_a, **_k: {"free_cash": 50_000.0, "positions": []},
    )
    monkeypatch.setattr(portfolio_store, "_ensure_portfolio_exists", lambda *_a, **_k: None)
    monkeypatch.setattr(
        portfolio_store,
        "refresh_portfolio_total_equity",
        lambda *_a, **_k: EquityRefreshResult(True, 1, "ok"),
    )
    result = record_fill(
        "USER_LIVE:u1",
        Fill("600000", "buy", 100, price, "2026-05-15", name="浦发银行"),
        client=client,
    )
    assert result.ok is True
    return [call for call in client.calls if call["table"] == "portfolio_positions" and call["action"] == "insert"]


def test_record_fill_skips_stop_above_fill_price(monkeypatch):
    """票面止损 9.5、成交 9.05：写进持仓后下一轮 Step4 会把新仓强制 EXIT。"""
    client = _FakeClient(trade_orders=[_entry_ticket(9.5, "2026-05-15")])

    inserts = _record_new_buy(monkeypatch, client, 9.05)

    assert inserts
    assert "stop_loss" not in inserts[0]["payload"]


def test_record_fill_skips_stop_equal_to_fill_price(monkeypatch):
    client = _FakeClient(trade_orders=[_entry_ticket(9.05, "2026-05-15")])

    inserts = _record_new_buy(monkeypatch, client, 9.05)

    assert inserts
    assert "stop_loss" not in inserts[0]["payload"]
