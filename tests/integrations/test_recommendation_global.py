from __future__ import annotations

from types import SimpleNamespace

from integrations import recommendation_global as mod


class _Query:
    def __init__(self, client):
        self.client = client
        self.start = 0
        self.stop = 999

    def select(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, start: int, stop: int):
        self.start, self.stop = start, stop
        return self

    def upsert(self, rows, *, on_conflict: str):
        self.client.upserts.append((list(rows), on_conflict))
        return self

    def execute(self):
        return SimpleNamespace(data=self.client.rows[self.start : self.stop + 1])


class _Client:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.upserts: list[tuple[list[dict], str]] = []

    def table(self, _name: str):
        return _Query(self)


def test_global_upsert_uses_event_day_price(monkeypatch):
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(mod, "is_admin_configured", lambda: True)
    scenarios = [
        (
            [
                {"code": "AAPL.US", "recommend_date": 20260801, "initial_price": 200.0, "current_price": 210.0},
                {"code": "AAPL.US", "recommend_date": 20260802, "initial_price": 205.0, "current_price": 210.0},
            ],
            {"code": "AAPL.US", "name": "Apple", "latest_close": 220.0},
            "us",
            (220.0, 220.0, 0.0),
        ),
        (
            [{"code": "00700.HK", "recommend_date": 20260803, "initial_price": 500.0, "current_price": 530.0}],
            {"code": "00700.HK", "name": "Tencent", "latest_close": 510.0},
            "hk",
            (510.0, 530.0, round((530.0 - 510.0) / 510.0 * 100.0, 2)),
        ),
    ]

    for rows, recommendation, market, expected in scenarios:
        client = _Client(rows)
        monkeypatch.setattr(mod, "create_admin_client", lambda client=client: client)
        assert mod.upsert_global_recommendations(20260803, [recommendation], market) is True
        payload = client.upserts[0][0][0]
        assert (payload["initial_price"], payload["current_price"], payload["change_pct"]) == expected


def test_global_upsert_includes_springboard_fields_when_scored(monkeypatch):
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(mod, "is_admin_configured", lambda: True)
    client = _Client([])
    monkeypatch.setattr(mod, "create_admin_client", lambda: client)
    rec = {
        "code": "AAPL.US",
        "name": "Apple",
        "latest_close": 220.0,
        "springboard_a": True,
        "springboard_b": False,
        "springboard_c": True,
        "springboard_combo": "A+C",
        "springboard_grade": "A+C",
        "springboard_met_count": 2,
        "springboard_support": 208.5,
        "springboard_touch_count": 3,
        "springboard_evidence": {"touches": 3},
        "springboard_scored": True,
        "actionable": True,
        "entry_quality_grade": "A",
        "entry_quality_score": 76.0,
        "bias_20": 2.4,
        "bias_200": 11.5,
        "support_level": 208.5,
    }
    assert mod.upsert_global_recommendations(20260803, [rec], "us") is True
    payload = client.upserts[0][0][0]
    assert payload["springboard_combo"] == "A+C"
    assert payload["springboard_support"] == 208.5
    assert payload["springboard_scored"] is True
    assert payload["actionable"] is True
    assert payload["entry_quality_grade"] == "A"
    assert payload["springboard_evidence"]["entry_tier"]["actionable"] is True


def test_global_upsert_retries_without_missing_optional_columns(monkeypatch):
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(mod, "is_admin_configured", lambda: True)
    client = _Client([])
    attempts = 0
    orig_table = client.table

    def flaky_table(name: str):
        tbl = orig_table(name)
        orig_upsert = tbl.upsert

        def wrapped_upsert(rows, on_conflict=""):
            nonlocal attempts
            attempts += 1
            if attempts == 1 and any("bias_200" in r or "actionable" in r for r in rows):
                raise RuntimeError("Could not find the 'actionable' and 'bias_200' columns in schema cache")
            return orig_upsert(rows, on_conflict=on_conflict)

        tbl.upsert = wrapped_upsert
        return tbl

    client.table = flaky_table
    monkeypatch.setattr(mod, "create_admin_client", lambda: client)
    rec = {
        "code": "AAPL.US",
        "name": "Apple",
        "latest_close": 220.0,
        "springboard_combo": "A+B",
        "springboard_evidence": {},
        "springboard_scored": True,
        "actionable": True,
        "entry_quality_grade": "S",
        "bias_20": 2.1,
        "bias_200": 14.5,
    }
    assert mod.upsert_global_recommendations(20260803, [rec], "us") is True
    payload = client.upserts[0][0][0]
    assert "actionable" not in payload
    assert "bias_200" not in payload
    # Exact token match: missing 'bias_200' must not strip 'bias_20'
    assert payload["bias_20"] == 2.1
    assert payload["springboard_combo"] == "A+B"
    assert payload["springboard_evidence"]["entry_tier"]["actionable"] is True
