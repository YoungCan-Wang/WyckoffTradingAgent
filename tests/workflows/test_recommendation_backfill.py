from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflows import recommendation_backfill as workflow
from workflows.recommendation_backfill import RecommendationBackfillRequest


def test_backfill_dry_run_does_not_require_write_context(monkeypatch, tmp_path) -> None:
    guard_calls: list[str] = []

    monkeypatch.setattr(workflow, "require_server_write_context", lambda operation: guard_calls.append(operation))
    monkeypatch.setattr(workflow, "_build_day_result", lambda day, skip_step3: _day_result(day))
    monkeypatch.setattr(workflow, "_build_payloads", lambda _dates, _results: {20260601: [_payload_row(1, 20260601)]})
    monkeypatch.setattr(workflow, "create_admin_client", lambda: object())
    monkeypatch.setattr(workflow, "_fetch_target_rows", lambda _client, _dates: [])

    status = workflow.run_recommendation_backfill(
        RecommendationBackfillRequest(
            dates=(date(2026, 6, 1),),
            output_dir=str(tmp_path),
            apply=False,
        )
    )

    assert status == 0
    assert guard_calls == []
    assert (tmp_path / "summary.json").exists()


def test_backfill_apply_replaces_only_stale_rows_for_target_dates(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(workflow, "_refresh_performance", lambda: None)

    summary = workflow._replace_target_dates(
        client,
        {
            20260601: [
                _payload_row(1, 20260601),
                _payload_row(2, 20260601),
            ]
        },
        [
            {"id": "keep", "code": 1, "recommend_date": 20260601},
            {"id": "stale", "code": 3, "recommend_date": 20260601},
            {"id": "other-day", "code": 9, "recommend_date": 20260531},
        ],
    )

    assert summary["rows_upserted"] == 2
    assert summary["stale_deleted"] == 1
    assert client.upserts == [[_payload_row(1, 20260601), _payload_row(2, 20260601)]]
    assert client.deletes == [{"key": "id", "values": ["stale"], "filters": {}}]


def test_backfill_recommendation_upsert_drops_missing_optional_columns() -> None:
    client = _FakeClient(fail_optional_schema=True)
    row = {**_payload_row(1, 20260601), "capital_migration_bonus": 0.3}

    summary = workflow._replace_target_dates(client, {20260601: [row]}, [])

    assert summary["rows_upserted"] == 1
    assert client.upserts == [[_payload_row(1, 20260601)]]


def test_backfill_writes_artifacts_before_empty_date_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(workflow, "_build_day_result", lambda day, skip_step3: _day_result(day))
    monkeypatch.setattr(workflow, "_build_payloads", lambda _dates, _results: {20260601: []})
    monkeypatch.setattr(workflow, "create_admin_client", lambda: object())
    monkeypatch.setattr(workflow, "_fetch_target_rows", lambda _client, _dates: [])

    with pytest.raises(RuntimeError, match="生成结果存在空日期"):
        workflow.run_recommendation_backfill(
            RecommendationBackfillRequest(
                dates=(date(2026, 6, 1),),
                output_dir=str(tmp_path),
                apply=False,
            )
        )

    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "old_rows_backup.json").exists()
    assert (tmp_path / "recommendation_tracking_20260601.json").exists()
    assert (tmp_path / "table_row_counts.json").exists()


def test_day_env_forces_readonly_write_context(monkeypatch) -> None:
    monkeypatch.setenv("WYCKOFF_WRITE_CONTEXT", "server_job")
    monkeypatch.setenv("STEP3_SKIP_LLM", "original")

    with workflow._day_env(date(2026, 6, 1), skip_step3=True):
        assert os.environ["WYCKOFF_WRITE_CONTEXT"] == "cli"
        assert os.environ["FUNNEL_DYNAMIC_POLICY"] == "off"
        assert os.environ["STEP3_SKIP_LLM"] == "1"

    assert os.environ["WYCKOFF_WRITE_CONTEXT"] == "server_job"
    assert os.environ["STEP3_SKIP_LLM"] == "original"


def test_build_day_result_explicitly_disables_financial_metrics(monkeypatch) -> None:
    import workflows.wyckoff_funnel as funnel

    captured: dict = {}

    def fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return True, [], {"regime": "NEUTRAL"}, {"metrics": {}}

    monkeypatch.setattr(funnel, "run", fake_run)
    monkeypatch.setattr(
        workflow, "_prepare_symbols_for_recommendation", lambda *_args: SimpleNamespace(allow_ai_review=False)
    )
    monkeypatch.setattr(workflow, "recommendation_write_symbols", lambda *_args, **_kwargs: [])

    result = workflow._build_day_result(date(2026, 6, 1), skip_step3=True)

    assert captured["include_financial_metrics"] is False
    assert result["trade_date"] == "2026-06-01"


def test_backfill_summary_declares_operational_replay_context() -> None:
    result = workflow._summary(
        (date(2026, 6, 1),),
        [_day_result(date(2026, 6, 1))],
        {20260601: [_payload_row(1, 20260601)]},
        [],
    )

    assert result["replay_context"] == {
        "purpose": "operational_candidate_refresh",
        "price_data": "historical_as_of_trade_date",
        "metadata": "current_snapshot",
        "financial_metrics_requested": False,
        "dynamic_policy": "off",
        "point_in_time_backtest_safe": False,
    }


def test_recommendation_backfill_workflow_defaults_to_dry_run_and_uploads_artifacts() -> None:
    workflow_text = Path(".github/workflows/recommendation_backfill.yml").read_text(encoding="utf-8")

    assert "name: Recommendation Backfill" in workflow_text
    assert "apply:" in workflow_text
    assert "default: false" in workflow_text
    assert 'if [ "${{ inputs.apply }}" = "true" ]; then' in workflow_text
    assert "scripts/backfill_recommendation_tracking.py" in workflow_text
    assert "--skip-step3" in workflow_text
    assert "actions/upload-artifact@v4" in workflow_text
    assert "if: always()" in workflow_text


def test_backfill_rejects_empty_generated_dates_without_explicit_allow() -> None:
    with pytest.raises(RuntimeError, match="生成结果存在空日期"):
        workflow._validate_payloads({20260601: []}, allow_empty_date=False)

    workflow._validate_payloads({20260601: []}, allow_empty_date=True)


def test_replace_auxiliary_tables_replaces_date_scoped_rows(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(workflow, "upsert_signal_observations", lambda rows: len(rows))

    summary = workflow._replace_auxiliary_tables(
        client,
        (date(2026, 6, 1),),
        {
            "signal_pending": [{"code": 1, "signal_date": "2026-06-01", "signal_type": "mainline"}],
            "signal_observations": [{"market": "cn", "trade_date": "2026-06-01", "code": 1}],
            "external_seed_observations": [
                {"market": "cn", "trade_date": "2026-06-01", "source": "x", "code": "000001"}
            ],
            "market_signal_daily": [{"trade_date": "2026-06-01", "benchmark_regime": "NEUTRAL"}],
            "theme_radar_snapshot": [{"trade_date": "2026-06-01", "snapshot_json": {}}],
        },
    )

    assert summary["signal_pending_inserted"] == 1
    assert summary["signal_observations_upserted"] == 1
    assert summary["external_seed_upserted"] == 1
    assert summary["market_signal_upserted"] == 1
    assert summary["theme_radar_upserted"] == 1
    assert client.deletes[:3] == [
        {"key": "signal_date", "values": ["2026-06-01"], "filters": {}},
        {"key": "trade_date", "values": ["2026-06-01"], "filters": {"market": "cn"}},
        {"key": "trade_date", "values": ["2026-06-01"], "filters": {"market": "cn"}},
    ]
    assert client.upserts[0][0]["updated_at"]


def test_replace_auxiliary_tables_degrades_repair_regime_for_legacy_market_table(monkeypatch) -> None:
    client = _FakeClient(fail_market_regime_check=True)
    monkeypatch.setattr(workflow, "upsert_signal_observations", lambda rows: len(rows))

    summary = workflow._replace_auxiliary_tables(
        client,
        (date(2026, 6, 1),),
        {
            "signal_pending": [],
            "signal_observations": [],
            "external_seed_observations": [],
            "market_signal_daily": [
                {
                    "trade_date": "2026-06-01",
                    "benchmark_regime": "PANIC_REPAIR",
                    "source_jobs": {"daily_job": {"writer": "test"}},
                },
                {
                    "trade_date": "2026-06-02",
                    "benchmark_regime": "BEAR_REBOUND",
                    "source_jobs": {},
                },
            ],
            "theme_radar_snapshot": [],
        },
    )

    assert summary["market_signal_upserted"] == 2
    market_rows = client.upserts[0]
    assert [row["benchmark_regime"] for row in market_rows] == ["RISK_OFF", "RISK_OFF"]
    assert market_rows[0]["source_jobs"]["daily_job"] == {"writer": "test"}
    assert market_rows[0]["source_jobs"]["regime_compat"]["original_benchmark_regime"] == "PANIC_REPAIR"
    assert market_rows[1]["source_jobs"]["regime_compat"]["original_benchmark_regime"] == "BEAR_REBOUND"
    assert market_rows[1]["source_jobs"]["regime_compat"]["stored_benchmark_regime"] == "RISK_OFF"


def _day_result(day: date) -> dict:
    return {
        "trade_date": day.isoformat(),
        "recommend_date": int(day.strftime("%Y%m%d")),
        "raw_count": 1,
        "write_count": 1,
        "ai_codes": [],
        "springboard_updates": {},
        "benchmark_context": {},
        "symbols_info": [{"code": "000001", "name": "平安银行", "initial_price": 10.0}],
    }


def _payload_row(code: int, rec_date: int) -> dict:
    return {"code": code, "recommend_date": rec_date, "name": f"Stock{code}", "initial_price": 10.0}


class _FakeClient:
    def __init__(self, *, fail_optional_schema: bool = False, fail_market_regime_check: bool = False) -> None:
        self.fail_optional_schema = fail_optional_schema
        self.failed_optional_schema = False
        self.fail_market_regime_check = fail_market_regime_check
        self.failed_market_regime_check = False
        self.upserts: list[list[dict]] = []
        self.inserts: list[list[dict]] = []
        self.deletes: list[dict] = []

    def table(self, _name: str):
        return _FakeQuery(self)


class _FakeQuery:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.kind = ""
        self.payload: list[dict] = []
        self.filters_in: dict[str, list[object]] = {}
        self.filters_eq: dict[str, object] = {}

    def upsert(self, payload, **_kwargs):
        self.kind = "upsert"
        self.payload = list(payload)
        return self

    def insert(self, payload):
        self.kind = "insert"
        self.payload = list(payload)
        return self

    def delete(self):
        self.kind = "delete"
        return self

    def in_(self, key, values):
        self.filters_in[str(key)] = list(values)
        return self

    def eq(self, key, value):
        self.filters_eq[str(key)] = value
        return self

    def execute(self):
        if self.kind == "upsert":
            if (
                self.client.fail_optional_schema
                and not self.client.failed_optional_schema
                and any("capital_migration_bonus" in row for row in self.payload)
            ):
                self.client.failed_optional_schema = True
                raise Exception("Could not find the 'capital_migration_bonus' column")
            if (
                self.client.fail_market_regime_check
                and not self.client.failed_market_regime_check
                and any(row.get("benchmark_regime") in {"PANIC_REPAIR", "BEAR_REBOUND"} for row in self.payload)
            ):
                self.client.failed_market_regime_check = True
                raise Exception('violates check constraint "market_signal_daily_benchmark_regime_check"')
            self.client.upserts.append(self.payload)
            return SimpleNamespace(data=self.payload)
        if self.kind == "insert":
            self.client.inserts.append(self.payload)
            return SimpleNamespace(data=self.payload)
        if self.kind == "delete":
            key, values = next(iter(self.filters_in.items()))
            self.client.deletes.append({"key": key, "values": values, "filters": dict(self.filters_eq)})
        return SimpleNamespace(data=[])
