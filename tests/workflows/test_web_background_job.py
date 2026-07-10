from __future__ import annotations

import json
import sys
from argparse import Namespace
from datetime import date
from types import ModuleType

from workflows import web_background_job as workflow


class _Scalar:
    def __init__(self, value: float):
        self.value = value

    def item(self) -> float:
        return self.value


def test_load_payload_accepts_only_dict_json() -> None:
    assert workflow._load_payload('{"job": "x"}') == {"job": "x"}
    assert workflow._load_payload("[1, 2]") == {}
    assert workflow._load_payload("") == {}


def test_write_result_sanitizes_dates(tmp_path) -> None:
    target = tmp_path / "result.json"

    workflow._write_result(
        str(target),
        {
            "today": date(2026, 6, 23),
            "items": {1, 2},
            "nan_float": float("nan"),
            "inf_float": float("inf"),
            "scalar_nan": _Scalar(float("nan")),
        },
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["today"] == "2026-06-23"
    assert sorted(payload["items"]) == [1, 2]
    assert payload["nan_float"] is None
    assert payload["inf_float"] is None
    assert payload["scalar_nan"] is None


def test_run_funnel_screen_sanitizes_nonfinite_trigger_scores(monkeypatch) -> None:
    fake_pipeline = ModuleType("workflows.wyckoff_funnel")

    def fake_run_funnel(*_args, **_kwargs):
        return (
            True,
            [],
            {},
            {
                "metrics": {},
                "triggers": {"sos": [("000001", float("inf")), ("000002", float("nan")), ("000003", "bad")]},
                "name_map": {"000001": "平安银行"},
                "sector_map": {"000001": "银行"},
            },
        )

    fake_pipeline.run = fake_run_funnel
    monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)

    result = workflow._run_funnel_screen("req-score", {})

    assert result["trigger_groups"]["sos"] == [
        {"code": "000001", "name": "平安银行", "industry": "银行", "score": 0.0},
        {"code": "000002", "name": "000002", "industry": "未知行业", "score": 0.0},
        {"code": "000003", "name": "000003", "industry": "未知行业", "score": 0.0},
    ]


def test_run_funnel_screen_filters_low_quality_report_handoff(monkeypatch) -> None:
    fake_pipeline = ModuleType("workflows.wyckoff_funnel")

    def fake_run_funnel(*_args, **_kwargs):
        return (
            True,
            [
                {"code": "000001", "name": "强候选", "candidate_shadow_score": 88.0},
                {"code": "000002", "name": "弱候选", "candidate_shadow_score": 65.0},
            ],
            {},
            {
                "metrics": {"total_symbols": 2, "fetch_ok": 2},
                "triggers": {"sos": [("000001", 10.0), ("000002", 9.0)]},
                "name_map": {"000001": "强候选", "000002": "弱候选"},
                "selected_for_ai": ["000001", "000002"],
                "trade_mode": {
                    "mode": "risk_on",
                    "action": "允许候选进入AI复核",
                    "allow_ai_review": True,
                    "allow_recommendation_write": False,
                },
            },
        )

    fake_pipeline.run = fake_run_funnel
    monkeypatch.setitem(sys.modules, "workflows.wyckoff_funnel", fake_pipeline)

    result = workflow._run_funnel_screen("req-quality", {})

    assert [row["code"] for row in result["symbols_for_report"]] == ["000001"]
    assert [row["code"] for row in result["report_candidates"]] == ["000001"]
    assert [row["code"] for row in result["watch_candidates"]] == ["000002"]
    assert result["selected_for_ai"] == ["000001", "000002"]
    assert result["quality_gate"]["blocked_count"] == 1
    assert result["quality_gate"]["reason"] == "000002 弱候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    assert result["selection_brief"]["status"] == "ready_for_ai_review"
    assert result["selection_brief"]["primary_pick"]["code"] == "000001"
    assert result["action_plan"]["review_targets"]["tool"] == "generate_ai_report"
    assert result["next_tool"]["args"] == {"stock_codes": ["000001"]}


def test_run_web_background_job_writes_error_for_unknown_kind(tmp_path) -> None:
    target = tmp_path / "result.json"
    args = Namespace(job_kind="unknown", request_id="req1", payload_json='{"user_id":"u1"}', output=str(target))

    status = workflow.run_web_background_job(args)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert status == 1
    assert payload["status"] == "error"
    assert payload["request_id"] == "req1"
    assert "不支持的 job_kind" in payload["error"]


def test_run_web_background_job_runs_recommendation_event_eval(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_build(request):
        captured["request"] = request
        return {
            "metadata": {"market": request.market, "horizon_days": request.horizon_days},
            "summary": {
                "all": {"rows_ready": 12, "rows_total": 20, "hit_rate_pct": 60.0},
                "ranking_decision": {
                    "status": "candidate",
                    "recommended_strategy": "candidate_shadow_then_score",
                    "recommended_top_k": 1,
                    "reason": "candidate_shadow_then_score top1 passed lift and risk gates",
                },
            },
            "daily": [{"recommend_date": 20260601, "hit_rate_pct": 100.0}],
            "policy_selection": {
                "selection_strategy": "candidate_shadow_then_score",
                "recommend_date": 20260601,
                "picks": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "action_status": "ready_for_ai_review",
                        "label_ready": False,
                    }
                ],
            },
            "events": [],
        }

    monkeypatch.setattr("workflows.recommendation_event_eval.build_recommendation_event_eval", fake_build)
    target = tmp_path / "result.json"
    args = Namespace(
        job_kind="recommendation_event_eval",
        request_id="req2",
        payload_json='{"market":"cn","horizon_days":5,"target_pct":10,"max_dates":7,"top_k":[1,3]}',
        output=str(target),
    )

    status = workflow.run_web_background_job(args)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert status == 0
    assert payload["status"] == "success"
    assert payload["job_kind"] == "recommendation_event_eval"
    assert payload["summary"]["all"]["hit_rate_pct"] == 60.0
    assert payload["policy_selection"]["picks"][0]["code"] == "300750"
    assert payload["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
    assert payload["candidate_guard_summary"]["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    assert "ranking_decision=candidate" in payload["result_summary"]
    assert "candidate_shadow_then_score top1" in payload["result_summary"]
    assert "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代" in payload["result_summary"]
    assert captured["request"].max_dates == 7
    assert captured["request"].top_k == (1, 3)
