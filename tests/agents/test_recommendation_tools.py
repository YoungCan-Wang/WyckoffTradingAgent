from __future__ import annotations

from agents.recommendation_tools import evaluate_recommendation_events


def _fake_eval_result(request):
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
        "policy_selection": {
            "status": "candidate",
            "selection_strategy": "candidate_shadow_then_score",
            "top_k": 1,
            "recommend_date": 20260601,
            "uses_promoted_ranking": True,
            "action_plan": {
                "primary_action": "generate_ai_report",
                "candidate_action": "generate_ai_report",
                "new_buy_allowed": False,
                "ai_review_allowed": True,
                "trade_readiness": "research_only",
                "review_status": "ready_for_ai_review",
                "reason": "只读推荐事件评估已通过排序接入门槛，可进入 AI 研报；不直接触发买入",
                "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                "next_tool": {
                    "tool": "generate_ai_report",
                    "args": {"stock_codes": ["300750"]},
                    "reason": "推荐事件评估只读候选已过排序门槛，先生成 AI 研报再做攻防决策",
                },
            },
            "picks": [
                {
                    "rank": 1,
                    "code": "300750",
                    "name": "宁德时代",
                    "candidate_shadow_score": 92.0,
                    "candidate_shadow_grade": "S",
                    "entry_quality_score": 84.0,
                    "entry_quality_grade": "A",
                    "entry_quality_risk_flags": ["短线涨幅偏快"],
                    "candidate_quality_score": 92.0,
                    "risk_adjusted_quality_score": 87.0,
                    "entry_risk_penalty": 5.0,
                    "action_status": "ready_for_ai_review",
                    "quality_factors": ["候选影子评级 S", "入场质量评级 A"],
                    "risk_factors": ["最新候选的未来窗口标签尚未成熟"],
                    "next_step": "生成 AI 研报并结合持仓形成攻防决策",
                    "label_ready": False,
                    "label_status": "partial_window",
                }
            ],
        },
        "daily": [{"recommend_date": 20260601, "hit_rate_pct": 100.0}],
        "events": [],
    }


def test_evaluate_recommendation_events_returns_policy_selection(monkeypatch):
    captured = {}

    def fake_build(request):
        captured["request"] = request
        return _fake_eval_result(request)

    monkeypatch.setattr("workflows.recommendation_event_eval.build_recommendation_event_eval", fake_build)

    result = evaluate_recommendation_events(market="cn", top_k="1,3")

    assert result["ok"] is True
    assert result["job_kind"] == "recommendation_event_eval"
    assert result["policy_selection"]["picks"][0]["code"] == "300750"
    assert result["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
    assert result["candidate_guard_summary"]["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    assert "ranking_decision=candidate" in result["result_summary"]
    assert "最新候选(20260601, candidate_shadow_then_score): 300750 宁德时代" in result["result_summary"]
    assert "风险调整分87" in result["result_summary"]
    assert "状态=可进入AI研报" in result["result_summary"]
    assert captured["request"].top_k == (1, 3)


def test_evaluate_recommendation_events_records_report_handoff(monkeypatch):
    from agents.tool_context import ToolContext

    monkeypatch.setattr("workflows.recommendation_event_eval.build_recommendation_event_eval", _fake_eval_result)
    ctx = ToolContext({})

    result = evaluate_recommendation_events(tool_context=ctx)

    assert ctx.state["last_recommendation_event_eval"]["policy_selection"]["picks"][0]["code"] == "300750"
    assert ctx.state["last_recommendation_event_eval"]["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
    handoff = ctx.state["last_screen_result"]
    assert handoff["scan_scope"]["source"] == "recommendation_event_eval"
    assert handoff["selection_brief"]["status"] == "ready_for_ai_review"
    assert handoff["selection_brief"]["best_codes"] == ["300750"]
    assert handoff["action_plan"]["new_buy_allowed"] is False
    assert handoff["action_plan"]["ai_review_allowed"] is True
    assert handoff["action_plan"]["trade_readiness"] == "research_only"
    assert handoff["action_plan"]["review_targets"]["tool"] == "generate_ai_report"
    assert handoff["summary"]["report_candidates"] == 1
    assert handoff["summary"]["watch_candidates"] == 0
    assert handoff["symbols_for_report"][0]["candidate_quality_score"] == 92.0
    assert handoff["report_candidates"][0]["code"] == "300750"
    assert handoff["symbols_for_report"][0]["risk_adjusted_quality_score"] == 87.0
    assert handoff["symbols_for_report"][0]["entry_risk_penalty"] == 5.0
    assert handoff["symbols_for_report"][0]["candidate_shadow_grade"] == "S"
    assert handoff["symbols_for_report"][0]["action_status"] == "ready_for_ai_review"
    assert handoff["symbols_for_report"][0]["trade_readiness"] == "research_only"
    assert handoff["symbols_for_report"][0]["new_buy_allowed"] is False
    assert "短线涨幅偏快" in handoff["symbols_for_report"][0]["risk_factors"]
    assert "最新候选的未来窗口标签尚未成熟" in handoff["symbols_for_report"][0]["risk_factors"]
    assert handoff["candidate_guard_summary"]["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"
    assert handoff["selection_brief"]["tool_handoff"]["args"]["stock_codes"][0] == "300750"
    assert result["policy_selection"]["picks"][0]["code"] == "300750"


def test_recommendation_eval_handoff_blocks_research_only_direct_buy_when_label_ready(monkeypatch):
    from agents.tool_context import ToolContext

    def fake_eval_result(request):
        result = _fake_eval_result(request)
        pick = result["policy_selection"]["picks"][0]
        pick["label_ready"] = True
        pick["label_status"] = "ready"
        pick["risk_factors"] = []
        return result

    monkeypatch.setattr("workflows.recommendation_event_eval.build_recommendation_event_eval", fake_eval_result)
    ctx = ToolContext({})

    result = evaluate_recommendation_events(tool_context=ctx)
    handoff = ctx.state["last_screen_result"]

    assert result["candidate_guard_summary"]["direct_buy_blocked_count"] == 1
    assert result["candidate_guard_summary"]["candidates"][0]["reason"] == "候选未开放新增买入，禁止直接买入"
    assert handoff["symbols_for_report"][0]["trade_readiness"] == "research_only"
    assert handoff["symbols_for_report"][0]["new_buy_allowed"] is False
    assert handoff["candidate_guard_summary"]["candidates"][0]["reason"] == "候选未开放新增买入，禁止直接买入"


def test_recommendation_eval_watch_only_handoff_blocks_auto_report(monkeypatch):
    from agents.report_tools import generate_ai_report
    from agents.tool_context import ToolContext

    ctx = ToolContext({})
    result = _fake_eval_result(type("Request", (), {"market": "cn", "horizon_days": 5})())
    result["summary"]["ranking_decision"] = {"status": "keep_score_only"}
    result["policy_selection"].update(
        {
            "status": "keep_score_only",
            "uses_promoted_ranking": False,
            "action_plan": {
                "primary_action": "watch_latest_policy_selection",
                "candidate_action": "watch_only",
                "new_buy_allowed": False,
                "ai_review_allowed": False,
                "trade_readiness": "research_only",
                "review_status": "watch_only",
                "reason": "只读推荐事件评估未通过排序接入门槛，继续观察；不直接触发买入",
                "next_step": "先作为观察候选复核，等待更多样本或研报证据后再升级",
            },
        }
    )
    result["policy_selection"]["picks"][0]["action_status"] = "watch_only"
    monkeypatch.setattr("workflows.recommendation_event_eval.build_recommendation_event_eval", lambda _request: result)
    evaluate_recommendation_events(tool_context=ctx)
    monkeypatch.setattr("agents.report_tools.ensure_tushare_token", lambda _tool_context: None)

    report = generate_ai_report(tool_context=ctx)
    handoff = ctx.state["last_screen_result"]

    assert handoff["selection_brief"]["status"] == "watch_only"
    assert "状态=只读观察" in ctx.state["last_recommendation_event_eval"]["result_summary"]
    assert handoff["action_plan"]["ai_review_allowed"] is False
    assert handoff["action_plan"]["watch_candidates"][0]["code"] == "300750"
    assert handoff["summary"]["report_candidates"] == 0
    assert handoff["summary"]["watch_candidates"] == 1
    assert handoff["symbols_for_report"] == []
    assert handoff["report_candidates"] == []
    assert handoff["watch_candidates"][0]["code"] == "300750"
    assert report["status"] == "blocked_by_policy_guard"
    assert report["error"].startswith("上一轮候选仍是只读观察")
    assert "未通过排序接入门槛" in report["error"]


def test_evaluate_recommendation_events_surfaces_config_error(monkeypatch):
    def fake_build(_request):
        raise ValueError("TICKFLOW_API_KEY 未配置")

    monkeypatch.setattr("workflows.recommendation_event_eval.build_recommendation_event_eval", fake_build)

    result = evaluate_recommendation_events()

    assert "TICKFLOW_API_KEY 未配置" in result["error"]
    assert "SUPABASE_URL" in result["hint"]
