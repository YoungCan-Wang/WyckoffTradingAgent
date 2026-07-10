from __future__ import annotations

from workflows import step4_from_supabase as workflow


def test_resolve_recommend_date_normalizes_input_and_env(monkeypatch):
    monkeypatch.setenv("STEP4_RECOMMEND_DATE", "2026-06-21")
    assert workflow.resolve_recommend_date("") == 20260621
    assert workflow.resolve_recommend_date("20260622") == 20260622
    assert workflow.resolve_recommend_date("2026-06-23") == 20260623


def test_load_recommendations_maps_rows(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "fetch_recommendation_rows",
        lambda _date: [
            {
                "code": "390",
                "name": "晨光",
                "recommend_reason": "confirmed",
                "funnel_score": 88,
                "priority_score": 91,
                "capital_migration_bonus": 4.5,
                "selection_source": "二次确认",
                "candidate_lane": "mainline",
                "is_ai_recommended": True,
            },
            {"code": "bad", "name": "坏行", "funnel_score": 10, "is_ai_recommended": False},
            {"code": "", "name": "空代码", "is_ai_recommended": True},
        ],
    )

    symbols_info, ai_codes = workflow.load_recommendations(20260621)

    assert [item["code"] for item in symbols_info] == ["000390", "bad"]
    assert symbols_info[0]["priority_score"] == 91
    assert symbols_info[0]["capital_migration_bonus"] == 4.5
    assert symbols_info[0]["selection_source"] == "二次确认"
    assert symbols_info[0]["candidate_lane"] == "mainline"
    assert symbols_info[0]["source_type"] == "supabase_recommendation_tracking"
    assert ai_codes == ["000390"]
    report = workflow.build_external_report(20260621, symbols_info, ai_codes)
    assert "capital_migration=4.5" in report


def test_run_step4_from_supabase_delegates_pipeline(monkeypatch):
    logs: list[str] = []
    captured: dict = {}
    monkeypatch.setattr(workflow, "resolve_logs_path", lambda _raw: "logs/step4.log")
    monkeypatch.setattr(workflow, "log_line", lambda msg, _path: logs.append(msg))
    monkeypatch.setattr(
        workflow,
        "load_recommendations",
        lambda _date: ([{"code": "000390", "name": "晨光", "tag": "confirmed", "funnel_score": 88}], ["000390"]),
    )
    monkeypatch.setattr(workflow, "load_step4_target", lambda: ({"user_id": "u1", "portfolio_id": "p1"}, "ok"))
    monkeypatch.setattr(workflow, "resolve_provider_name", lambda *_args: "efficiency")
    monkeypatch.setattr(workflow, "get_provider_credentials", lambda _provider: ("key", "model", "base"))
    monkeypatch.setattr(workflow, "run_step4_pipeline", lambda **kwargs: captured.update(kwargs) or {"ok": True})

    result = workflow.run_step4_from_supabase(workflow.Step4FromSupabaseRequest(recommend_date="20260621"))

    assert result == 0
    assert captured["step4_target"]["portfolio_id"] == "p1"
    assert captured["step3_springboard_codes"] == ["000390"]
    assert "000390" in captured["step3_report_text"]
    assert any("recommendation_rows=1" in item for item in logs)
