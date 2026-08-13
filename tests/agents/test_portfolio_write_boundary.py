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
    from integrations import supabase_portfolio as portfolio_store
    from integrations.supabase_portfolio import EquityRefreshResult, delete_position, update_free_cash, upsert_position

    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)
    monkeypatch.setattr(
        portfolio_store,
        "refresh_portfolio_total_equity",
        lambda *_args, **_kwargs: EquityRefreshResult(True, 2_000, "ok"),
    )
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

    result = update_portfolio(
        action="add", code="000001", name="平安银行", shares=-100, cost_price=10.0, buy_dt="2026-07-01"
    )

    assert result["error"] == "shares 不能为负数"


def test_update_portfolio_rejects_negative_cost_price():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(
        action="add", code="000001", name="平安银行", shares=100, cost_price=-1.0, buy_dt="2026-07-01"
    )

    assert result["error"] == "cost_price 不能为负数"


def test_update_portfolio_rejects_negative_free_cash():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="set_cash", free_cash=-500.0)

    assert result["error"] == "free_cash 不能为负数"


def test_update_portfolio_add_requires_buy_dt(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from agents.portfolio_tools import MISSING_BUY_DT_ERROR
    from integrations import local_db

    if local_db._conn is not None:
        local_db._conn.close()
        local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "portfolio.db")
    local_db.init_db()
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: "天华新能")

    result = portfolio_tools.update_portfolio(action="add", code="300390", name="天华新能", shares=200, cost_price=64.9)

    assert result["error"] == MISSING_BUY_DT_ERROR
    state = local_db.load_portfolio("LOCAL")
    assert not state or not state.get("positions")


def test_update_portfolio_preserves_buy_dt_when_editing_size_or_cost(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from integrations import local_db

    if local_db._conn is not None:
        local_db._conn.close()
        local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "portfolio.db")
    local_db.init_db()
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: "平安银行")

    added = portfolio_tools.update_portfolio(
        action="add",
        code="000001",
        name="平安银行",
        shares=100,
        cost_price=10.0,
        buy_dt="2026-07-01",
    )
    assert added.get("success") is True

    updated = portfolio_tools.update_portfolio(
        action="update", code="000001", name="平安银行", shares=200, cost_price=10.5
    )
    assert updated.get("success") is True
    assert local_db.load_portfolio("LOCAL")["positions"][0]["buy_dt"] == "2026-07-01"
