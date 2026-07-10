from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Response:
    data: list[dict] | None = None


class _FakeQuery:
    def __init__(self, client: _FakeClient):
        self.client = client
        self.filters: list[tuple[str, str]] = []
        self.order_column = ""
        self.order_desc = False
        self.limit_value: int | None = None
        self.update_payload: dict | None = None

    def select(self, _columns: str):
        return self

    def update(self, payload: dict):
        self.update_payload = payload
        return self

    def eq(self, column: str, value: str):
        self.filters.append((column, value))
        return self

    def order(self, column: str, *, desc: bool = False):
        self.order_column = column
        self.order_desc = desc
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def execute(self):
        rows = self.client.rows
        for column, value in self.filters:
            rows = [row for row in rows if str(row.get(column)) == str(value)]
        if self.update_payload is not None:
            for row in rows:
                row.update(self.update_payload)
            self.client.updates.append({"filters": list(self.filters), "payload": dict(self.update_payload)})
            return _Response(data=rows)
        if self.order_column:
            rows = sorted(rows, key=lambda row: row.get(self.order_column, ""), reverse=self.order_desc)
        if self.limit_value is not None:
            rows = rows[: self.limit_value]
        return _Response(data=rows)


class _FakeClient:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.table_calls = 0
        self.last_query: _FakeQuery | None = None
        self.updates: list[dict] = []

    def table(self, _name: str):
        self.table_calls += 1
        self.last_query = _FakeQuery(self)
        return self.last_query


def _tail_buy_rows() -> list[dict]:
    return [
        {
            "user_id": "user-a",
            "code": "600001",
            "name": "A",
            "run_date": "2026-05-10",
            "signal_date": "2026-05-09",
            "signal_type": "spring",
            "final_decision": "BUY",
            "rule_score": 90,
            "priority_score": 95,
            "rule_reasons": "",
            "llm_decision": "BUY",
            "llm_reason": "",
        },
        {
            "user_id": "user-b",
            "code": "600002",
            "name": "B",
            "run_date": "2026-05-10",
            "signal_date": "2026-05-09",
            "signal_type": "sos",
            "final_decision": "BUY",
            "rule_score": 80,
            "priority_score": 85,
            "rule_reasons": "",
            "llm_decision": "BUY",
            "llm_reason": "",
        },
    ]


def test_load_tail_buy_from_supabase_requires_user_id(monkeypatch):
    from integrations import supabase_tail_buy

    monkeypatch.delenv("SUPABASE_USER_ID", raising=False)
    monkeypatch.setattr(
        supabase_tail_buy,
        "_read",
        lambda: (_ for _ in ()).throw(AssertionError("read client should not be created")),
    )

    assert supabase_tail_buy.load_tail_buy_from_supabase(limit=10) == []


def test_load_tail_buy_from_supabase_filters_user(monkeypatch):
    from integrations import supabase_tail_buy

    client = _FakeClient(_tail_buy_rows())
    monkeypatch.delenv("SUPABASE_USER_ID", raising=False)
    monkeypatch.setattr(supabase_tail_buy, "_read", lambda: client)

    rows = supabase_tail_buy.load_tail_buy_from_supabase(limit=10, user_id="user-a")

    assert [row["code"] for row in rows] == ["600001"]
    assert client.last_query is not None
    assert client.last_query.filters == [("user_id", "user-a")]


def test_sync_tail_buy_requires_user_id_before_query(monkeypatch):
    from integrations import sync

    client = _FakeClient(_tail_buy_rows())
    monkeypatch.delenv("SUPABASE_USER_ID", raising=False)

    assert sync.sync_tail_buy(client=client) == 0
    assert client.table_calls == 0


def test_sync_tail_buy_filters_user_before_local_write(monkeypatch):
    from integrations import sync

    client = _FakeClient(_tail_buy_rows())
    captured: list[dict] = []
    meta: dict[str, int] = {}

    def fake_save(rows: list[dict]) -> int:
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr("integrations.local_db.save_tail_buy_results", fake_save)
    monkeypatch.setattr("integrations.local_db.update_sync_meta", lambda table, count: meta.update({table: count}))

    assert sync.sync_tail_buy(client=client, user_id="user-a") == 1
    assert client.last_query is not None
    assert client.last_query.filters == [("user_id", "user-a")]
    assert [row["code"] for row in captured] == ["600001"]
    assert meta == {"tail_buy_history": 1}


def test_tail_buy_payload_includes_entry_and_current_prices():
    from integrations.supabase_tail_buy import _payload_row

    payload = _payload_row(
        {
            "code": "600001",
            "name": "A",
            "run_date": "2026-05-10",
            "initial_price": 10.5,
            "current_price": 10.8,
            "change_pct": 2.86,
            "price_updated_at": "2026-05-10T07:00:00+00:00",
            "features_json": '{"last_close":10.5}',
        },
        "user-a",
    )

    assert payload["initial_price"] == 10.5
    assert payload["current_price"] == 10.8
    assert payload["change_pct"] == 2.86
    assert payload["price_updated_at"] == "2026-05-10T07:00:00+00:00"
    assert payload["features_json"] == {"last_close": 10.5}


def test_refresh_tail_buy_prices_updates_current_price(monkeypatch):
    from integrations import supabase_tail_buy

    rows = [
        {
            "user_id": "user-a",
            "code": "600001",
            "run_date": "2026-05-10",
            "initial_price": 10.0,
            "last_close": 9.8,
        },
        {
            "user_id": "user-b",
            "code": "600002",
            "run_date": "2026-05-10",
            "initial_price": 20.0,
            "last_close": 20.0,
        },
    ]
    client = _FakeClient(rows)
    captured: dict[str, object] = {}

    def fake_fetch_quotes(api_key: str, symbols: list[str], batch_size: int) -> dict[str, dict]:
        captured.update({"api_key": api_key, "symbols": symbols, "batch_size": batch_size})
        return {"600001.SH": {"last_price": 11.2}}

    monkeypatch.setenv("TICKFLOW_API_KEY", "tick-key")
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(supabase_tail_buy, "_configured", lambda: True)
    monkeypatch.setattr(supabase_tail_buy, "_admin", lambda: client)
    monkeypatch.setattr(supabase_tail_buy, "_fetch_tail_quotes", fake_fetch_quotes)

    summary = supabase_tail_buy.refresh_tail_buy_prices_with_tickflow_realtime(user_id="user-a")

    assert summary["rows_total"] == 1
    assert summary["rows_updated"] == 1
    assert captured["symbols"] == ["600001.SH"]
    assert rows[0]["current_price"] == 11.2
    assert rows[0]["change_pct"] == 12.0
    assert rows[0]["price_updated_at"]
    assert rows[1].get("current_price") is None


def test_refresh_tail_buy_prices_skips_rows_without_user_id(monkeypatch):
    from integrations import supabase_tail_buy

    rows = [
        {
            "code": "600001",
            "run_date": "2026-05-10",
            "initial_price": 10.0,
            "last_close": 10.0,
        },
        {
            "user_id": "user-a",
            "code": "600001",
            "run_date": "2026-05-10",
            "initial_price": 20.0,
            "last_close": 20.0,
        },
    ]
    client = _FakeClient(rows)

    monkeypatch.setenv("TICKFLOW_API_KEY", "tick-key")
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(supabase_tail_buy, "_configured", lambda: True)
    monkeypatch.setattr(supabase_tail_buy, "_admin", lambda: client)
    monkeypatch.setattr(
        supabase_tail_buy,
        "_fetch_tail_quotes",
        lambda *_args: {"600001.SH": {"last_price": 22.0}},
    )

    summary = supabase_tail_buy.refresh_tail_buy_prices_with_tickflow_realtime()

    assert summary["rows_total"] == 2
    assert summary["rows_updated"] == 1
    assert rows[0].get("current_price") is None
    assert rows[1]["current_price"] == 22.0
    assert client.updates[0]["filters"] == [
        ("code", "600001"),
        ("run_date", "2026-05-10"),
        ("user_id", "user-a"),
    ]
