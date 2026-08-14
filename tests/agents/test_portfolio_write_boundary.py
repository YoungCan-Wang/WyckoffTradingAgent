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

    def insert(self, payload, **_kwargs):
        self.action = "insert"
        self.payload = payload
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
        return SimpleNamespace(
            data=[] if self.action == "select" else self.client.response_data(self.action, self.payload)
        )


class _FakeUserClient:
    def __init__(self, *, update_rows: list | None = None):
        self.calls: list[dict] = []
        self.table_name = ""
        self.update_rows = update_rows

    def response_data(self, action: str, payload):
        if action == "update":
            return [] if self.update_rows is not None else [payload]
        return [payload]

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
    assert actions == ["update", "delete", "select", "upsert", "update"]


def test_portfolio_admin_fallback_rejects_cli_context(monkeypatch):
    from integrations.supabase_portfolio import upsert_position

    monkeypatch.delenv("WYCKOFF_WRITE_CONTEXT", raising=False)

    ok, msg = upsert_position("USER_LIVE:u1", {"code": "000001", "shares": 100, "cost_price": 10})

    assert ok is False
    assert "server_job" in msg


def test_update_portfolio_rejects_non_positive_shares():
    from agents.portfolio_tools import update_portfolio

    negative = update_portfolio(
        action="add", code="000001", name="平安银行", shares=-100, cost_price=10.0, buy_dt="2026-07-01"
    )
    zero = update_portfolio(
        action="add", code="000001", name="平安银行", shares=0, cost_price=10.0, buy_dt="2026-07-01"
    )

    assert negative["error"] == "shares 必须大于 0"
    assert zero["error"] == "shares 必须大于 0"


def test_update_portfolio_rejects_non_positive_cost_price():
    from agents.portfolio_tools import update_portfolio

    negative = update_portfolio(
        action="add", code="000001", name="平安银行", shares=100, cost_price=-1.0, buy_dt="2026-07-01"
    )
    zero = update_portfolio(
        action="add", code="000001", name="平安银行", shares=100, cost_price=0.0, buy_dt="2026-07-01"
    )

    assert negative["error"] == "cost_price 必须大于 0"
    assert zero["error"] == "cost_price 必须大于 0"


def test_update_portfolio_rejects_negative_free_cash():
    from agents.portfolio_tools import update_portfolio

    result = update_portfolio(action="set_cash", free_cash=-500.0)

    assert result["error"] == "free_cash 不能为负数"


def test_update_portfolio_add_requires_buy_dt(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from core.buy_dt import MISSING_BUY_DT_ERROR
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


def _local_portfolio(monkeypatch, tmp_path):
    from agents import portfolio_tools
    from integrations import local_db

    if local_db._conn is not None:
        local_db._conn.close()
        local_db._conn = None
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "portfolio.db")
    local_db.init_db()
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx=None: False)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx=None: "LOCAL")
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)
    return portfolio_tools, local_db


def test_update_portfolio_rejects_invalid_buy_dt(monkeypatch, tmp_path):
    from core.buy_dt import INVALID_BUY_DT_ERROR

    portfolio_tools, local_db = _local_portfolio(monkeypatch, tmp_path)
    result = portfolio_tools.update_portfolio(
        action="add", code="300390", name="天华新能", shares=200, cost_price=64.9, buy_dt="yesterday"
    )
    assert result["error"] == INVALID_BUY_DT_ERROR
    state = local_db.load_portfolio("LOCAL")
    assert not state or not state.get("positions")


def test_update_portfolio_does_not_create_missing_position(monkeypatch, tmp_path):
    from core.buy_dt import POSITION_MISSING_ERROR

    portfolio_tools, local_db = _local_portfolio(monkeypatch, tmp_path)
    result = portfolio_tools.update_portfolio(
        action="update", code="300390", name="天华新能", shares=200, cost_price=64.9
    )
    assert result["error"] == POSITION_MISSING_ERROR
    state = local_db.load_portfolio("LOCAL")
    assert not state or not state.get("positions")


def test_insert_position_requires_valid_buy_dt(monkeypatch):
    from core.buy_dt import INVALID_BUY_DT_ERROR, MISSING_BUY_DT_ERROR
    from integrations import supabase_portfolio as portfolio_store
    from integrations.supabase_portfolio import insert_position

    monkeypatch.setattr(
        portfolio_store,
        "refresh_portfolio_total_equity",
        lambda *_args, **_kwargs: type("R", (), {"ok": True, "message": "ok"})(),
    )
    monkeypatch.setattr(portfolio_store, "_ensure_portfolio_exists", lambda *_a, **_k: None)

    client = _FakeUserClient()
    missing, msg = insert_position("USER_LIVE:u1", {"code": "000001", "shares": 100, "cost_price": 10}, client=client)
    assert missing is False
    assert msg == MISSING_BUY_DT_ERROR
    assert "insert" not in [call["action"] for call in client.calls]

    client = _FakeUserClient()
    ok, msg = insert_position(
        "USER_LIVE:u1",
        {"code": "000001", "shares": 100, "cost_price": 10, "buy_dt": "2026-13-40"},
        client=client,
    )
    assert ok is False
    assert msg == INVALID_BUY_DT_ERROR
    assert "insert" not in [call["action"] for call in client.calls]


def test_update_position_does_not_insert_when_missing(monkeypatch):
    from core.buy_dt import POSITION_MISSING_ERROR
    from integrations import supabase_portfolio as portfolio_store
    from integrations.supabase_portfolio import update_position

    monkeypatch.setattr(portfolio_store, "_ensure_portfolio_exists", lambda *_a, **_k: None)
    client = _FakeUserClient(update_rows=[])
    ok, msg = update_position(
        "USER_LIVE:u1",
        {"code": "000001", "name": "平安银行", "shares": 200, "cost_price": 10.5},
        client=client,
    )
    assert ok is False
    assert msg == POSITION_MISSING_ERROR
    assert [call["action"] for call in client.calls] == ["update"]
    assert "insert" not in [call["action"] for call in client.calls]
    assert "upsert" not in [call["action"] for call in client.calls]


def test_upsert_position_missing_without_buy_dt_does_not_insert(monkeypatch):
    from core.buy_dt import MISSING_BUY_DT_ERROR
    from integrations.supabase_portfolio import upsert_position

    client = _FakeUserClient(update_rows=[])
    ok, msg = upsert_position(
        "USER_LIVE:u1",
        {"code": "000001", "name": "平安银行", "shares": 200, "cost_price": 10.5, "buy_dt": ""},
        client=client,
    )
    assert ok is False
    assert msg == MISSING_BUY_DT_ERROR
    assert [call["action"] for call in client.calls] == ["update"]
    assert "insert" not in [call["action"] for call in client.calls]
    assert "upsert" not in [call["action"] for call in client.calls]


def test_upsert_position_existing_omits_empty_buy_dt(monkeypatch):
    from integrations import supabase_portfolio as portfolio_store
    from integrations.supabase_portfolio import EquityRefreshResult, upsert_position

    monkeypatch.setattr(
        portfolio_store,
        "refresh_portfolio_total_equity",
        lambda *_args, **_kwargs: EquityRefreshResult(True, 2_000, "ok"),
    )
    client = _FakeUserClient()
    ok, _msg = upsert_position(
        "USER_LIVE:u1",
        {"code": "000001", "name": "平安银行", "shares": 200, "cost_price": 10.5, "buy_dt": ""},
        client=client,
    )
    assert ok is True
    update_calls = [call for call in client.calls if call["action"] == "update"]
    assert update_calls
    assert "buy_dt" not in update_calls[0]["payload"]
    assert "insert" not in [call["action"] for call in client.calls]
    assert "upsert" not in [call["action"] for call in client.calls]


def test_record_fill_new_without_buy_dt_creates_nothing(monkeypatch):
    from core.buy_dt import MISSING_BUY_DT_ERROR
    from core.trade_fill import Fill
    from integrations import supabase_portfolio as portfolio_store
    from integrations.supabase_portfolio import record_fill

    client = _FakeUserClient(update_rows=[])
    monkeypatch.setattr(portfolio_store, "_resolve_write_client", lambda _client, _action: client)
    monkeypatch.setattr(
        portfolio_store,
        "load_portfolio_state",
        lambda *_a, **_k: {"free_cash": 50_000.0, "positions": []},
    )
    result = record_fill(
        "USER_LIVE:u1",
        Fill("000001", "buy", 100, 10.0, "", name="平安银行"),
        client=client,
    )
    assert result.ok is False
    assert MISSING_BUY_DT_ERROR in result.message
    assert not [call for call in client.calls if call["table"] == "portfolio_positions"]


def test_record_fill_existing_without_buy_dt_preserves_date(monkeypatch):
    from core.trade_fill import Fill
    from integrations import supabase_portfolio as portfolio_store
    from integrations.supabase_portfolio import EquityRefreshResult, record_fill

    client = _FakeUserClient()
    monkeypatch.setattr(portfolio_store, "_resolve_write_client", lambda _client, _action: client)
    monkeypatch.setattr(
        portfolio_store,
        "load_portfolio_state",
        lambda *_a, **_k: {
            "free_cash": 50_000.0,
            "positions": [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "shares": 1000,
                    "cost": 10.0,
                    "buy_dt": "20260101",
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
        Fill("000001", "buy", 100, 10.0, "", name="平安银行"),
        client=client,
    )
    assert result.ok is True
    position_writes = [call for call in client.calls if call["table"] == "portfolio_positions"]
    assert position_writes
    assert all(call["action"] == "update" for call in position_writes)
    assert "buy_dt" not in position_writes[0]["payload"]
