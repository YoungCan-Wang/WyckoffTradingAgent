from __future__ import annotations

from types import SimpleNamespace


class _FakeQuery:
    def __init__(self, client):
        self.client = client
        self.action = ""
        self.payload = None
        self.filters: list[tuple[str, str]] = []

    def select(self, *_args, **_kwargs):
        self.action = "select"
        return self

    def upsert(self, payload, **_kwargs):
        self.action = "upsert"
        self.payload = payload
        return self

    def delete(self):
        self.action = "delete"
        return self

    def update(self, payload):
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, column: str, value):
        self.filters.append((column, str(value)))
        return self

    def limit(self, _value: int):
        return self

    def execute(self):
        self.client.calls.append(
            {
                "table": self.client.table_name,
                "action": self.action,
                "payload": self.payload,
                "filters": list(self.filters),
            }
        )
        return SimpleNamespace(data=[] if self.action == "select" else [self.payload])


class _FakeUserClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.table_name = ""

    def table(self, name: str):
        self.table_name = name
        return _FakeQuery(self)


def test_portfolio_writes_accept_explicit_user_client(monkeypatch):
    from integrations.supabase_portfolio import delete_position, update_free_cash, upsert_position

    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)
    client = _FakeUserClient()

    ok, _ = upsert_position("USER_LIVE:u1", {"code": "000001", "shares": 100, "cost_price": 10}, client=client)
    assert ok is True

    ok, _ = delete_position("USER_LIVE:u1", "000001", client=client)
    assert ok is True

    ok, _ = update_free_cash("USER_LIVE:u1", 1000, client=client)
    assert ok is True

    actions = [call["action"] for call in client.calls]
    assert actions == ["select", "upsert", "upsert", "delete", "select", "upsert", "update"]


def test_portfolio_admin_fallback_rejects_cli_context(monkeypatch):
    from integrations.supabase_portfolio import upsert_position

    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)

    ok, msg = upsert_position("USER_LIVE:u1", {"code": "000001", "shares": 100, "cost_price": 10})

    assert ok is False
    assert "server_job" in msg


def test_update_portfolio_rejects_negative_shares():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="add", code="000001", name="平安银行", shares=-100, cost_price=10.0)

    assert result["error"] == "shares 不能为负数"


def test_update_portfolio_rejects_negative_cost_price():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="add", code="000001", name="平安银行", shares=100, cost_price=-1.0)

    assert result["error"] == "cost_price 不能为负数"


def test_update_portfolio_rejects_negative_free_cash():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="set_cash", free_cash=-500.0)

    assert result["error"] == "free_cash 不能为负数"
