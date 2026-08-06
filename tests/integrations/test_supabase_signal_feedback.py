from __future__ import annotations

from integrations import supabase_signal_feedback as mod


class _Response:
    def __init__(self, data: list[dict] | None = None):
        self.data = data or []


class _RangeCapturingQuery:
    """模拟服务端硬性单次行数上限：无论请求多少行，超过 server_cap 一律截断。"""

    def __init__(self, all_rows: list[dict], server_cap: int):
        self._all_rows = all_rows
        self._server_cap = server_cap
        self.ranges_requested: list[tuple[int, int]] = []
        self._start = 0
        self._stop = 0

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def gte(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, start: int, stop: int):
        self._start, self._stop = start, stop
        self.ranges_requested.append((start, stop))
        return self

    def execute(self):
        requested = self._stop - self._start + 1
        served = min(requested, self._server_cap)
        return _Response(self._all_rows[self._start : self._start + served])


class _FakeAdminClient:
    def __init__(self, all_rows: list[dict], server_cap: int):
        self.query = _RangeCapturingQuery(all_rows, server_cap)

    def table(self, _name: str):
        return self.query


def _make_rows(n: int) -> list[dict]:
    return [{"id": i, "trade_date": "2026-06-01", "code": f"{i:06d}"} for i in range(n)]


def test_fetch_paginated_collects_all_rows_beyond_server_page_cap():
    all_rows = _make_rows(2500)
    client = _FakeAdminClient(all_rows, server_cap=1000)

    result = mod._fetch_paginated(lambda: client.table("x"), limit=20000, page_size=1000)

    assert len(result) == 2500
    assert [row["id"] for row in result] == list(range(2500))


def test_fetch_paginated_respects_requested_limit():
    all_rows = _make_rows(5000)
    client = _FakeAdminClient(all_rows, server_cap=1000)

    result = mod._fetch_paginated(lambda: client.table("x"), limit=1500, page_size=1000)

    assert len(result) == 1500


def test_fetch_paginated_stops_when_source_exhausted_before_limit():
    all_rows = _make_rows(150)
    client = _FakeAdminClient(all_rows, server_cap=1000)

    result = mod._fetch_paginated(lambda: client.table("x"), limit=20000, page_size=1000)

    assert len(result) == 150


def test_fetch_paginated_empty_source_returns_empty():
    client = _FakeAdminClient([], server_cap=1000)

    result = mod._fetch_paginated(lambda: client.table("x"), limit=20000, page_size=1000)

    assert result == []


def test_load_recent_signal_outcomes_fetches_beyond_single_page_cap(monkeypatch):
    all_rows = _make_rows(3425)
    client = _FakeAdminClient(all_rows, server_cap=1000)
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "_admin", lambda: client)
    monkeypatch.setattr(mod, "_close", lambda _client: None)

    rows = mod.load_recent_signal_outcomes(days=180, limit=20000, market="cn")

    assert len(rows) == 3425


def test_load_recent_signal_observations_fetches_beyond_single_page_cap(monkeypatch):
    all_rows = _make_rows(2177)
    client = _FakeAdminClient(all_rows, server_cap=1000)
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "_admin", lambda: client)
    monkeypatch.setattr(mod, "_close", lambda _client: None)

    rows = mod.load_recent_signal_observations(days=90, limit=5000, market="cn")

    assert len(rows) == 2177


class _MutationQuery:
    def __init__(self, calls: list[tuple], existing: list[dict] | None = None):
        self.calls = calls
        self._existing = existing or []
        self._mode = "none"

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        self.calls.append(("select",))
        return self

    def delete(self):
        self._mode = "delete"
        self.calls.append(("delete",))
        return self

    def eq(self, column, value):
        self.calls.append(("eq", column, value))
        return self

    def upsert(self, rows, on_conflict):
        self._mode = "upsert"
        self.calls.append(("upsert", rows, on_conflict))
        return self

    def execute(self):
        self.calls.append(("execute", self._mode))
        if self._mode == "select":
            return _Response(self._existing)
        return _Response()


class _MutationClient:
    def __init__(self, existing: list[dict] | None = None):
        self.calls: list[tuple] = []
        self._existing = existing or []

    def table(self, table):
        self.calls.append(("table", table))
        return _MutationQuery(self.calls, self._existing)


def _patch_mutation_client(monkeypatch, client: _MutationClient) -> None:
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "_admin", lambda: client)
    monkeypatch.setattr(mod, "_close", lambda _client: None)
    monkeypatch.setattr(mod, "require_server_write_context", lambda _operation: None)


def test_replace_signal_health_removes_stale_snapshot_rows(monkeypatch):
    existing = [
        {"market": "cn", "as_of_date": "2026-08-04", "signal_type": "sos", "regime": "", "horizon_days": 5},
        {"market": "cn", "as_of_date": "2026-08-04", "signal_type": "spring", "regime": "", "horizon_days": 5},
    ]
    client = _MutationClient(existing)
    _patch_mutation_client(monkeypatch, client)
    rows = [
        {
            "market": "cn",
            "as_of_date": "2026-08-04",
            "signal_type": "sos",
            "regime": "",
            "horizon_days": 5,
        }
    ]

    written = mod.replace_signal_health(rows, market="cn", as_of_date="2026-08-04")

    assert written == 1
    assert ("eq", "market", "cn") in client.calls
    assert ("eq", "as_of_date", "2026-08-04") in client.calls
    assert any(call[0] == "upsert" for call in client.calls)
    upsert_at = next(i for i, call in enumerate(client.calls) if call[0] == "upsert")
    delete_at = next(i for i, call in enumerate(client.calls) if call[0] == "delete")
    assert upsert_at < delete_at
    assert ("eq", "signal_type", "spring") in client.calls


def test_replace_signal_registry_refuses_untrusted_empty_snapshot(monkeypatch):
    client = _MutationClient()
    _patch_mutation_client(monkeypatch, client)

    written = mod.replace_signal_registry([], market="cn")

    assert written == 0
    assert client.calls == []


def test_replace_signal_registry_can_clear_only_with_explicit_override(monkeypatch):
    existing = [{"market": "cn", "signal_type": "sos", "regime": ""}]
    client = _MutationClient(existing)
    _patch_mutation_client(monkeypatch, client)

    written = mod.replace_signal_registry([], market="cn", allow_empty=True)

    assert written == 0
    assert ("eq", "market", "cn") in client.calls
    assert ("eq", "signal_type", "sos") in client.calls
    assert any(call[0] == "delete" for call in client.calls)
    assert not any(call[0] == "upsert" for call in client.calls)


def test_replace_signal_registry_upserts_before_deleting_orphans(monkeypatch):
    existing = [
        {"market": "cn", "signal_type": "sos", "regime": ""},
        {"market": "cn", "signal_type": "spring", "regime": ""},
    ]
    client = _MutationClient(existing)
    _patch_mutation_client(monkeypatch, client)
    rows = [{"market": "cn", "signal_type": "sos", "regime": "", "status": "ACTIVE"}]

    written = mod.replace_signal_registry(rows, market="cn")

    assert written == 1
    upsert_at = next(i for i, call in enumerate(client.calls) if call[0] == "upsert")
    delete_at = next(i for i, call in enumerate(client.calls) if call[0] == "delete")
    assert upsert_at < delete_at
    assert ("eq", "signal_type", "spring") in client.calls


def test_strict_outcome_load_raises_instead_of_returning_empty(monkeypatch):
    monkeypatch.setattr(mod, "_configured", lambda: True)
    monkeypatch.setattr(mod, "_admin", lambda: (_ for _ in ()).throw(OSError("temporary outage")))

    import pytest

    with pytest.raises(RuntimeError, match="failed to load signal outcomes"):
        mod.load_recent_signal_outcomes(raise_on_error=True)
