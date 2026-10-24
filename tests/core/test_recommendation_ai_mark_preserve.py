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

        def suppress_shared_side_effects(self) -> bool:
            return self.preview_only

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

        def suppress_shared_side_effects(self) -> bool:
            return self.preview_only

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


def test_parse_step3_springboards_empty_input_is_not_authoritative(monkeypatch):
    """Hard-defense / allow_ai_review=False sends zero symbols into Step3.

    The empty status report still says 处于起跳板/- 无, and extract short-circuits
    to [] when allowed_codes is empty. That must not become authoritative [] and
    wipe same-day is_ai_recommended from a morning success.
    """
    calls: list[object] = []

    def _extract(**_kwargs):
        calls.append("extract")
        return []

    monkeypatch.setattr("tools.report_parser.extract_operation_pool_codes", _extract)
    monkeypatch.setattr("tools.report_parser.extract_operation_pool_springboards", lambda **_kwargs: {})
    empty_report = (
        "# 🏛️ Alpha 投委会机密电报：威科夫盘面审判\n\n"
        "> ⚪ **本轮未执行三阵营模型审判**：上游实际送入 Step3 的候选为 0。\n\n"
        "## 🏹 处于起跳板\n"
        "- 无（未执行模型审判，不表示任何风险结论）"
    )
    assert parse_step3_springboards(empty_report, [], None) is None
    assert parse_step3_springboards(empty_report, [{"code": ""}], None) is None
    assert calls == []


def test_run_step3_stage_empty_input_does_not_authoritative_mark():
    from workflows.daily_job_step3 import run_step3_stage

    class _Cfg:
        logs_path = None
        webhook = ""
        api_key = "k"
        model = "m"
        provider = "p"
        llm_base_url = ""
        wecom_webhook = ""
        dingtalk_webhook = ""

    def fake_run_step3(symbols_info, *_args, **_kwargs):
        assert symbols_info == []
        return True, "ok", "# 空研报\n## 🏹 处于起跳板\n- 无"

    result = run_step3_stage(
        symbols_info=[],
        benchmark_context={"regime": "CRASH"},
        run_step3=fake_run_step3,
        cfg=_Cfg(),
    )
    assert result.summary_item["ok"] is True
    assert result.ai_mark_authoritative is False
    assert result.springboard_codes == []
