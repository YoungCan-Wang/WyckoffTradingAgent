"""Regression: same-day funnel re-run must not wipe is_ai_recommended."""

from __future__ import annotations

from core.recommendation_payload import build_recommendation_payload
from workflows.daily_job_common import Step3StageResult
from workflows.daily_job_step3 import mark_step3_outputs, parse_step3_springboards


def test_step2_payload_omits_is_ai_recommended():
    rows = build_recommendation_payload(
        20261004,
        [{"code": "600519", "name": "贵州茅台", "initial_price": 1800.0, "score": 9.0}],
        {},
        {},
    )
    assert rows
    assert "is_ai_recommended" not in rows[0]


def test_mark_step3_outputs_skips_when_not_authoritative(monkeypatch):
    calls: list[object] = []

    class _Cfg:
        logs_path = None
        preview_only = False

    monkeypatch.setattr(
        "workflows.daily_job_persistence.mark_step3_recommendations",
        lambda *args, **kwargs: calls.append(("mark", args, kwargs)),
    )
    step3 = Step3StageResult(
        report_text="",
        springboard_codes=[],
        springboard_updates={},
        summary_item={"ok": False, "err": "llm_failed"},
        ai_mark_authoritative=False,
    )
    mark_step3_outputs(20261004, [{"code": 600519}], step3, _Cfg())
    assert calls == []


def test_mark_step3_outputs_runs_when_authoritative_empty_pool(monkeypatch):
    calls: list[object] = []

    class _Cfg:
        logs_path = None
        preview_only = False

    monkeypatch.setattr(
        "workflows.daily_job_persistence.mark_step3_recommendations",
        lambda *args, **kwargs: calls.append(args[1]),
    )
    monkeypatch.setattr(
        "workflows.daily_signal_observations.apply_step3_springboard_updates",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "workflows.daily_job_persistence.write_recommendation_backup",
        lambda *args, **kwargs: None,
    )
    step3 = Step3StageResult(
        report_text="ok",
        springboard_codes=[],
        springboard_updates={},
        summary_item={"ok": True, "err": None},
        ai_mark_authoritative=True,
    )
    mark_step3_outputs(20261004, [{"code": 600519}], step3, _Cfg())
    assert calls == [[]]


def test_parse_step3_springboards_returns_none_on_failure(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("tools.report_parser.extract_operation_pool_codes", _boom)
    assert parse_step3_springboards("report", [{"code": "600519"}], None) is None
