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


def test_global_upsert_preserves_first_price_across_new_and_same_day_runs(monkeypatch):
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr(mod, "is_admin_configured", lambda: True)
    scenarios = [
        (
            [
                {"code": "AAPL.US", "recommend_date": 20260801, "initial_price": 200.0},
                {"code": "AAPL.US", "recommend_date": 20260802, "initial_price": 205.0},
            ],
            {"code": "AAPL.US", "name": "Apple", "latest_close": 220.0},
            "us",
            (200.0, 220.0, 10.0),
        ),
        (
            [{"code": "00700.HK", "recommend_date": 20260803, "initial_price": 500.0}],
            {"code": "00700.HK", "name": "Tencent", "latest_close": 510.0},
            "hk",
            (500.0, 510.0, 2.0),
        ),
    ]

    for rows, recommendation, market, expected in scenarios:
        client = _Client(rows)
        monkeypatch.setattr(mod, "create_admin_client", lambda client=client: client)
        assert mod.upsert_global_recommendations(20260803, [recommendation], market) is True
        payload = client.upserts[0][0][0]
        assert (payload["initial_price"], payload["current_price"], payload["change_pct"]) == expected
