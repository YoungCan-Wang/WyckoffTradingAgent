from __future__ import annotations


class _Response:
    def __init__(self, data: list[dict] | None = None):
        self.data = data or []


class _FakeTable:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.payload: list[dict] | None = None
        self.conflict = ""
        self.limit_value: int | None = None
        self.deleted_filters: list[tuple[str, str]] = []
        self.kind = ""

    def upsert(self, payload: list[dict], *, on_conflict: str):
        self.kind = "upsert"
        self.payload = payload
        self.conflict = on_conflict
        return self

    def delete(self):
        self.kind = "delete"
        return self

    def eq(self, column: str, value: str):
        if self.kind == "delete":
            self.deleted_filters.append((column, value))
        return self

    def select(self, _columns: str):
        self.kind = "select"
        return self

    def order(self, _column: str, *, desc: bool = False):
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def execute(self):
        if self.kind in {"delete", "upsert"}:
            return _Response()
        rows = self.rows[: self.limit_value] if self.limit_value is not None else self.rows
        return _Response(rows)


class _FakeClient:
    def __init__(self, rows: list[dict] | None = None):
        self.table_obj = _FakeTable(rows or [])
        self.table_name = ""

    def table(self, name: str):
        self.table_name = name
        return self.table_obj


def test_upsert_concept_heat_history_sorts_and_limits(monkeypatch):
    from integrations import supabase_concept_heat as mod

    client = _FakeClient()
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "_admin", lambda: client)

    written = mod.upsert_concept_heat_history(
        "2026-05-25",
        [
            {"name": "低流入", "pct": 1.0, "net_inflow": 10, "cid": "a"},
            {"name": "高流入", "pct": 2.0, "net_inflow": 20, "cid": "b"},
            {"name": "证金持股", "pct": 9.0, "net_inflow": 900, "cid": "noise"},
        ],
        top_n=1,
    )

    assert written == 1
    assert client.table_name == "concept_heat_history"
    assert client.table_obj.deleted_filters == [("trade_date", "2026-05-25")]
    assert client.table_obj.conflict == "trade_date,concept_name"
    assert client.table_obj.payload == [
        {
            "trade_date": "2026-05-25",
            "concept_name": "高流入",
            "pct": 2.0,
            "net_inflow": 20.0,
            "rank": 1,
            "source_id": "b",
        }
    ]


def test_load_concept_heat_history_groups_recent_days(monkeypatch):
    from integrations import supabase_concept_heat as mod

    client = _FakeClient(
        [
            {"trade_date": "2026-05-25", "concept_name": "A", "pct": 1, "net_inflow": 10, "rank": 1},
            {"trade_date": "2026-05-24", "concept_name": "B", "pct": 2, "net_inflow": 20, "rank": 1},
            {"trade_date": "2026-05-23", "concept_name": "C", "pct": 3, "net_inflow": 30, "rank": 1},
        ]
    )
    monkeypatch.setattr(mod, "_read", lambda: client)

    history = mod.load_concept_heat_history_from_supabase(limit_days=2)

    assert list(history) == ["2026-05-25", "2026-05-24"]
    assert history["2026-05-25"]["A"] == {"pct": 1.0, "inflow": 10.0}
