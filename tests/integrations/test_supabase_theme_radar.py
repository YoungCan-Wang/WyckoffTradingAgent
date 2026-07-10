from __future__ import annotations


class _Response:
    def __init__(self, data: list[dict] | None = None):
        self.data = data or []


class _FakeTable:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.payload: dict | None = None
        self.conflict = ""
        self.limit_value: int | None = None

    def upsert(self, payload: dict, *, on_conflict: str):
        self.payload = payload
        self.conflict = on_conflict
        return self

    def select(self, _columns: str):
        return self

    def order(self, _column: str, *, desc: bool = False):
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def execute(self):
        if self.payload is not None:
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


def test_upsert_theme_radar_snapshot_writes_summary_columns(monkeypatch):
    from integrations import supabase_theme_radar as mod

    client = _FakeClient()
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "_admin", lambda: client)

    written = mod.upsert_theme_radar_snapshot(
        {
            "trade_date": "2026-05-27",
            "themes": [{"theme": "芯片半导体"}],
            "strategic_candidates": [{"code": "000001"}],
        }
    )

    assert written == 1
    assert client.table_name == "theme_radar_snapshot"
    assert client.table_obj.conflict == "trade_date"
    assert client.table_obj.payload["top_themes"] == ["芯片半导体"]
    assert client.table_obj.payload["top_candidates"] == ["000001"]


def test_load_latest_theme_radar_snapshot_from_supabase(monkeypatch):
    from integrations import supabase_theme_radar as mod

    client = _FakeClient([{"snapshot_json": {"trade_date": "2026-05-27"}}])
    monkeypatch.setattr(mod, "_read", lambda: client)

    snapshot = mod.load_latest_theme_radar_snapshot_from_supabase()

    assert snapshot == {"trade_date": "2026-05-27"}
