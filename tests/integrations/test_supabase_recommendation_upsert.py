from __future__ import annotations

import json
from types import SimpleNamespace

from integrations.recommendation_payload import (
    mark_ai_recommendations,
    upsert_recommendation_payload_rows,
    upsert_recommendations,
    write_recommendation_backup_artifact,
)


class FakeSupabaseClient:
    def __init__(
        self,
        rows: list[dict] | None = None,
        *,
        fail_select: bool = False,
        fail_optional_schema: bool = False,
    ) -> None:
        self.rows = rows or []
        self.fail_select = fail_select
        self.fail_optional_schema = fail_optional_schema
        self.failed_optional_schema = False
        self.upserts: list[list[dict]] = []
        self.updates: list[dict] = []

    def table(self, _name: str):
        return FakeSupabaseQuery(self)


class FakeSupabaseQuery:
    def __init__(self, client: FakeSupabaseClient) -> None:
        self.client = client
        self.kind = ""
        self.payload: list[dict] = []
        self.filters_eq: dict[str, object] = {}
        self.filters_in: dict[str, list[object]] = {}

    def select(self, *_args, **_kwargs):
        self.kind = "select"
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self

    def upsert(self, payload, **_kwargs):
        self.kind = "upsert"
        self.payload = list(payload)
        return self

    def update(self, payload, **_kwargs):
        self.kind = "update"
        self.payload = dict(payload)
        return self

    def eq(self, key, value):
        self.filters_eq[str(key)] = value
        return self

    def in_(self, key, values):
        self.filters_in[str(key)] = list(values)
        return self

    def execute(self):
        if self.kind == "select":
            if self.client.fail_select:
                raise RuntimeError("transient fetch failure")
            return SimpleNamespace(data=self.client.rows)
        if self.kind == "update":
            record = {"payload": self.payload, "eq": self.filters_eq, "in": self.filters_in}
            self.client.updates.append(record)
            return SimpleNamespace(data=[record])
        if (
            self.client.fail_optional_schema
            and not self.client.failed_optional_schema
            and any("capital_migration_bonus" in row for row in self.payload)
        ):
            self.client.failed_optional_schema = True
            raise Exception("Could not find the 'capital_migration_bonus' column")
        self.client.upserts.append(self.payload)
        return SimpleNamespace(data=self.payload)


def _enable_fake_supabase(monkeypatch, client: FakeSupabaseClient) -> None:
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setattr("integrations.recommendation_payload.is_supabase_configured", lambda: True)
    monkeypatch.setattr("integrations.recommendation_payload._get_supabase_admin_client", lambda: client)


def test_upsert_recommendations_aborts_when_history_fetch_fails(monkeypatch):
    client = FakeSupabaseClient(fail_select=True)
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(20260518, [{"code": "000001", "name": "Ping An", "initial_price": 10.0}])

    assert ok is False
    assert client.upserts == []


def test_upsert_recommendations_preserves_existing_recommend_count(monkeypatch):
    client = FakeSupabaseClient(
        rows=[
            {"code": 1, "recommend_count": 3, "recommend_date": 20260517},
            {"code": 1, "recommend_count": 2, "recommend_date": 20260516},
        ]
    )
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(20260518, [{"code": "000001", "name": "Ping An", "initial_price": 10.0}])

    assert ok is True
    assert client.upserts[0][0]["recommend_count"] == 4


def test_upsert_recommendations_dedupes_same_code_same_date(monkeypatch):
    client = FakeSupabaseClient()
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(
        20260518,
        [
            {
                "code": "600203",
                "name": "福日电子",
                "initial_price": 10.0,
                "funnel_score": 6.0,
                "primary_signal": "evr",
                "selection_source": "l3_fill",
                "priority_rank": 3,
            },
            {
                "code": "600203",
                "name": "福日电子",
                "initial_price": 10.5,
                "funnel_score": 9.0,
                "primary_signal": "sos",
                "signal_types": ["sos", "lps"],
                "selection_source": "l4_hit",
                "priority_rank": 1,
                "market_regime": "PANIC_REPAIR",
                "springboard_a": True,
                "springboard_b": True,
                "springboard_c": False,
                "springboard_combo": "A+B",
                "springboard_met_count": 2,
                "springboard_evidence": {"a_hits": [{"date": "2026-05-18"}]},
                "springboard_scored": True,
            },
        ],
    )

    assert ok is True
    assert len(client.upserts[0]) == 1
    assert client.upserts[0][0]["code"] == 600203
    assert client.upserts[0][0]["funnel_score"] == 9.0
    assert client.upserts[0][0]["primary_signal"] == "sos"
    assert client.upserts[0][0]["signal_types"] == ["sos", "lps"]
    assert client.upserts[0][0]["selection_source"] == "l4_hit"
    assert client.upserts[0][0]["selection_rank"] == 1
    assert client.upserts[0][0]["market_regime"] == "PANIC_REPAIR"
    assert client.upserts[0][0]["springboard_combo"] == "A+B"
    assert client.upserts[0][0]["springboard_met_count"] == 2
    assert client.upserts[0][0]["springboard_evidence"]["a_hits"][0]["date"] == "2026-05-18"


def test_upsert_recommendations_ignores_nonfinite_scores_when_deduping(monkeypatch):
    client = FakeSupabaseClient()
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(
        20260518,
        [
            {
                "code": "600203",
                "name": "福日电子",
                "initial_price": 10.0,
                "funnel_score": 6.0,
                "primary_signal": "evr",
                "selection_source": "l3_fill",
            },
            {
                "code": "600203",
                "name": "福日电子",
                "initial_price": 10.5,
                "funnel_score": float("inf"),
                "priority_score": float("nan"),
                "primary_signal": "sos",
                "selection_source": "l4_hit",
            },
        ],
    )

    assert ok is True
    row = client.upserts[0][0]
    assert row["funnel_score"] == 6.0
    assert row["primary_signal"] == "evr"
    assert row["selection_source"] == "l3_fill"


def test_upsert_recommendations_preserves_candidate_metadata(monkeypatch):
    client = FakeSupabaseClient()
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(
        20260625,
        [
            {
                "code": "300308",
                "name": "中际旭创",
                "initial_price": 100.0,
                "funnel_score": 86.0,
                "strategy_version": "candidate_lane_v1",
                "candidate_lane": "mainline",
                "entry_type": "主线平台再突破",
                "signal_key": "mainline",
                "candidate_status": "主线买点候选",
                "candidate_reasons": {"theme": "CPO"},
                "mainline_score": 0.86,
                "theme_score": 0.8,
                "timing_score": 0.72,
            }
        ],
    )

    assert ok is True
    row = client.upserts[0][0]
    assert row["strategy_version"] == "candidate_lane_v1"
    assert row["candidate_lane"] == "mainline"
    assert row["entry_type"] == "主线平台再突破"
    assert row["candidate_status"] == "主线买点候选"
    assert row["candidate_reasons"] == {"theme": "CPO"}
    assert row["mainline_score"] == 0.86
    assert row["timing_score"] == 0.72


def test_upsert_recommendations_fills_candidate_metadata_defaults(monkeypatch):
    client = FakeSupabaseClient()
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(
        20260625,
        [
            {
                "code": "600203",
                "name": "福日电子",
                "initial_price": 10.0,
                "primary_signal": "sos",
                "selection_source": "l4_hit",
            }
        ],
    )

    assert ok is True
    row = client.upserts[0][0]
    assert row["strategy_version"] == "candidate_lane_v1"
    assert row["candidate_lane"] == "sos"
    assert row["entry_type"] == "sos"
    assert row["signal_key"] == "sos"


def test_upsert_recommendations_writes_large_payload_in_chunks(monkeypatch):
    client = FakeSupabaseClient()
    _enable_fake_supabase(monkeypatch, client)
    symbols = [{"code": f"{idx:06d}", "name": f"Stock{idx}", "initial_price": 10.0} for idx in range(1, 1202)]

    ok = upsert_recommendations(20260518, symbols)

    assert ok is True
    assert [len(chunk) for chunk in client.upserts] == [500, 500, 201]


def test_upsert_payload_rows_drops_missing_optional_columns():
    client = FakeSupabaseClient(fail_optional_schema=True)
    row = {
        "code": 1,
        "recommend_date": 20260601,
        "name": "Stock1",
        "initial_price": 10.0,
        "capital_migration_bonus": 0.3,
    }

    upsert_recommendation_payload_rows(client, [row])

    assert client.upserts == [[{"code": 1, "recommend_date": 20260601, "name": "Stock1", "initial_price": 10.0}]]


def test_write_recommendation_backup_artifact_marks_ai_and_sql(tmp_path):
    rows = [
        {
            "code": 600203,
            "name": "Furi Electronics",
            "recommend_reason": "O'Reilly setup",
            "recommend_date": 20260526,
            "initial_price": 13.55,
            "current_price": 13.55,
            "change_pct": 0.0,
            "recommend_count": 2,
            "funnel_score": 9.0,
            "is_ai_recommended": False,
            "primary_signal": "sos",
            "signal_types": ["sos", "lps"],
            "selection_source": "l4_hit",
            "market_regime": "PANIC_REPAIR",
            "springboard_a": True,
            "springboard_b": False,
            "springboard_c": True,
            "springboard_combo": "A+C",
            "springboard_met_count": 2,
            "springboard_evidence": {"c_support": {"touch_dates": ["2026-05-24"]}},
            "springboard_scored": True,
            "updated_at": "2026-05-26T10:00:00+00:00",
        }
    ]

    paths = write_recommendation_backup_artifact(20260526, rows, str(tmp_path), ai_codes=["600203"])

    assert len(paths) == 2
    data = json.loads((tmp_path / "recommendation_tracking_20260526.json").read_text(encoding="utf-8"))
    assert data["row_count"] == 1
    assert data["rows"][0]["is_ai_recommended"] is True

    sql = (tmp_path / "recommendation_tracking_20260526.sql").read_text(encoding="utf-8")
    assert "insert into public.recommendation_tracking" in sql
    assert "on conflict (code, recommend_date) do update set" in sql
    assert "array['sos', 'lps']::text[]" in sql
    assert '\'{"c_support": {"touch_dates": ["2026-05-24"]}}\'::jsonb' in sql
    assert "'O''Reilly setup'" in sql


def test_write_recommendation_backup_artifact_sanitizes_nonfinite_numbers(tmp_path):
    rows = [
        {
            "code": 600203,
            "recommend_date": 20260526,
            "funnel_score": float("inf"),
            "priority_score": float("nan"),
            "updated_at": "2026-05-26T10:00:00+00:00",
        }
    ]

    write_recommendation_backup_artifact(20260526, rows, str(tmp_path))

    data = json.loads((tmp_path / "recommendation_tracking_20260526.json").read_text(encoding="utf-8"))
    assert data["rows"][0]["funnel_score"] is None
    assert data["rows"][0]["priority_score"] is None

    sql = (tmp_path / "recommendation_tracking_20260526.sql").read_text(encoding="utf-8")
    assert "Infinity" not in sql
    assert "NaN" not in sql


def test_mark_ai_recommendations_updates_step3_springboard_fields(monkeypatch):
    client = FakeSupabaseClient()
    _enable_fake_supabase(monkeypatch, client)

    ok = mark_ai_recommendations(
        20260617,
        ["603373"],
        springboard_updates={
            "603373": {
                "springboard_a": True,
                "springboard_b": False,
                "springboard_c": True,
                "springboard_combo": "A+C",
                "springboard_grade": "A+C",
                "springboard_met_count": 2,
                "springboard_evidence": {"source": "step3_report"},
                "springboard_scored": True,
            }
        },
    )

    assert ok is True
    assert client.updates[0]["payload"]["is_ai_recommended"] is False
    assert client.updates[1]["payload"]["is_ai_recommended"] is True
    assert client.updates[2]["eq"] == {"recommend_date": 20260617, "code": 603373}
    assert client.updates[2]["payload"]["springboard_combo"] == "A+C"
    assert client.updates[2]["payload"]["springboard_met_count"] == 2
