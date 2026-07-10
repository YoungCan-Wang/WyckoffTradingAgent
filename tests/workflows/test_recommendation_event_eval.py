from __future__ import annotations

from core.candidate_guards import policy_candidate_guard_summary
from workflows.recommendation_event_eval import (
    _build_summary,
    _observation_feature_map,
    _policy_selection,
    _policy_selection_markdown,
    _quality_feature_fields,
    _top_k_summary,
)


def test_top_k_summary_ranks_ai_then_score_then_count() -> None:
    events = [
        _event(20260515, "A", ai=False, score=0.99, count=3, hit=True),
        _event(20260515, "B", ai=True, score=0.30, count=1, hit=True),
        _event(20260515, "C", ai=False, score=0.80, count=1, hit=False),
        _event(20260516, "D", ai=False, score=0.70, count=2, hit=False),
        _event(20260516, "E", ai=False, score=0.90, count=1, hit=True),
    ]

    top1 = _top_k_summary(events, 1)
    top2_score = _top_k_summary(events, 2)
    top2_ai = _top_k_summary(events, 2, "ai_then_score")

    assert top1["rows_total"] == 2
    assert top1["hit_count"] == 2
    assert top1["hit_rate_pct"] == 100.0
    assert top2_score["rows_total"] == 4
    assert top2_score["hit_count"] == 2
    assert top2_score["hit_rate_pct"] == 50.0
    assert top2_ai["hit_count"] == 3
    assert top2_ai["hit_rate_pct"] == 75.0
    assert top2_ai["days_covered"] == 2


def test_top_k_summary_can_rank_by_candidate_quality_scores() -> None:
    events = [
        _event(20260515, "A", ai=False, score=0.99, count=3, hit=False, shadow=25.0, entry=30.0),
        _event(20260515, "B", ai=False, score=0.30, count=1, hit=True, shadow=88.0, entry=82.0),
        _event(20260516, "C", ai=False, score=0.95, count=2, hit=False, shadow=32.0, entry=35.0),
        _event(20260516, "D", ai=True, score=0.20, count=1, hit=True, shadow=80.0, entry=79.0),
    ]

    score_only = _top_k_summary(events, 1, "score_only")
    shadow_quality = _top_k_summary(events, 1, "candidate_shadow_then_score")
    entry_quality = _top_k_summary(events, 1, "entry_quality_then_score")
    summary = _build_summary(events, (1,))

    assert score_only["hit_rate_pct"] == 0.0
    assert shadow_quality["hit_rate_pct"] == 100.0
    assert entry_quality["hit_rate_pct"] == 100.0
    assert "candidate_shadow_then_score" in summary["top_k_by_strategy"]
    assert "entry_quality_then_score" in summary["top_k_by_strategy"]
    assert summary["top_k_lift_vs_score_only"]["candidate_shadow_then_score"]["1"]["hit_rate_delta_pct"] == 100.0
    assert summary["top_k_lift_vs_score_only"]["candidate_shadow_then_score"]["1"]["avg_mfe_delta_pct"] == 8.0
    assert summary["top_k_lift_vs_score_only"]["entry_quality_then_score"]["1"]["hit_rate_delta_pct"] == 100.0
    assert summary["ranking_decision"]["status"] == "insufficient_sample"


def test_summary_policy_and_daily_can_reuse_grouped_events(monkeypatch) -> None:
    from workflows import recommendation_event_eval as module

    events = [
        _event(20260515, "A", ai=False, score=0.99, count=2, hit=False, shadow=25.0),
        _event(20260515, "B", ai=False, score=0.30, count=1, hit=True, shadow=88.0),
        _event(20260516, "C", ai=False, score=0.80, count=1, hit=True, shadow=85.0),
    ]
    events_by_date = module._events_by_date(events)

    def fail_regroup(_events):
        raise AssertionError("events were regrouped")

    monkeypatch.setattr(module, "_events_by_date", fail_regroup)

    summary = module._build_summary(events, (1,), events_by_date)
    selection = module._policy_selection(events, summary["ranking_decision"], events_by_date)
    daily = module._daily_summary(events_by_date)

    assert summary["top_k"]["1"]["days_covered"] == 2
    assert selection["recommend_date"] == 20260516
    assert [row["recommend_date"] for row in daily] == [20260515, 20260516]


def test_build_summary_reuses_ranked_events_across_top_k(monkeypatch) -> None:
    from workflows import recommendation_event_eval as module

    events = [
        _event(20260515, "A", ai=False, score=0.99, count=2, hit=False, shadow=25.0),
        _event(20260515, "B", ai=False, score=0.30, count=1, hit=True, shadow=88.0),
        _event(20260516, "C", ai=False, score=0.80, count=1, hit=True, shadow=85.0),
        _event(20260516, "D", ai=True, score=0.40, count=3, hit=False, shadow=60.0),
    ]
    calls: list[tuple[str, tuple[str, ...]]] = []
    real_rank_events = module._rank_events

    def spy_rank_events(rows, strategy):
        calls.append((strategy, tuple(str(row["code"]) for row in rows)))
        return real_rank_events(rows, strategy)

    monkeypatch.setattr(module, "_rank_events", spy_rank_events)

    summary = module._build_summary(events, (1, 2, 3))

    assert summary["top_k"]["3"]["rows_total"] == 4
    assert len(calls) == len(module._RANKING_STRATEGIES) * 2
    assert len(set(calls)) == len(calls)


def test_policy_selection_ranks_candidate_quality_after_entry_risk_penalty() -> None:
    events = [
        {
            **_event(20260515, "RISKY", ai=False, score=0.99, count=2, hit=False, shadow=92.0, entry=84.0),
            "entry_quality_risk_flags": ["短线涨幅偏快"],
        },
        _event(20260515, "CLEAN", ai=False, score=0.30, count=1, hit=True, shadow=90.0, entry=82.0),
    ]
    decision = {
        "status": "candidate",
        "recommended_strategy": "candidate_shadow_then_score",
        "recommended_top_k": 1,
    }

    selection = _policy_selection(events, decision)

    assert [pick["code"] for pick in selection["picks"]] == ["CLEAN"]
    assert selection["picks"][0]["candidate_quality_score"] == 90.0
    assert selection["picks"][0]["risk_adjusted_quality_score"] == 90.0


def test_policy_selection_downgrades_low_adjusted_quality_latest_pick_to_watch() -> None:
    events = [
        _event(20260515, "LOW", ai=False, score=0.99, count=2, hit=False, shadow=65.0, entry=60.0),
        _event(20260515, "LOWER", ai=False, score=0.30, count=1, hit=True, shadow=40.0, entry=35.0),
    ]
    decision = {
        "status": "candidate",
        "recommended_strategy": "candidate_shadow_then_score",
        "recommended_top_k": 1,
        "reason": "candidate_shadow_then_score top1 passed lift and risk gates",
    }

    selection = _policy_selection(events, decision)

    assert selection["status"] == "watch"
    assert selection["uses_promoted_ranking"] is False
    assert selection["action_plan"]["ai_review_allowed"] is False
    assert selection["action_plan"]["candidate_action"] == "watch_only"
    assert "风险调整质量分 65.00 低于AI复核门槛 70.00" in selection["reason"]
    assert selection["picks"][0]["action_status"] == "watch_only"
    assert selection["picks"][0]["risk_adjusted_quality_score"] == 65.0
    assert "风险调整质量分 65.00 低于AI复核门槛 70.00" in selection["picks"][0]["risk_factors"][0]
    assert "next_tool" not in selection["action_plan"]


def test_policy_selection_markdown_surfaces_quality_gate_reason() -> None:
    events = [_event(20260515, "LOW", ai=False, score=0.99, count=2, hit=False, shadow=65.0, entry=60.0)]
    decision = {
        "status": "candidate",
        "recommended_strategy": "candidate_shadow_then_score",
        "recommended_top_k": 1,
        "reason": "candidate_shadow_then_score top1 passed lift and risk gates",
    }

    markdown = "\n".join(_policy_selection_markdown(_policy_selection(events, decision)))

    assert "- Policy status: `watch`" in markdown
    assert "- AI review allowed: `False`" in markdown
    assert "风险调整质量分 65.00 低于AI复核门槛 70.00" in markdown
    assert "| 1 | LOW | - | watch_only | N | 0.99 | 65.0 | 65.0 | 60.0 | -" in markdown


def test_policy_selection_keeps_candidate_when_quality_score_missing() -> None:
    events = [_event(20260515, "LEGACY", ai=False, score=0.99, count=2, hit=True)]
    decision = {
        "status": "candidate",
        "recommended_strategy": "candidate_shadow_then_score",
        "recommended_top_k": 1,
        "reason": "candidate_shadow_then_score top1 passed lift and risk gates",
    }

    selection = _policy_selection(events, decision)

    assert selection["status"] == "candidate"
    assert selection["uses_promoted_ranking"] is True
    assert selection["action_plan"]["ai_review_allowed"] is True
    assert selection["action_plan"]["next_tool"]["tool"] == "generate_ai_report"


def test_ranking_decision_recommends_quality_strategy_after_sample_gate() -> None:
    events = []
    for day in range(20260501, 20260513):
        events.append(_event(day, f"A{day}", ai=False, score=0.99, count=2, hit=False, shadow=25.0))
        events.append(_event(day, f"B{day}", ai=False, score=0.30, count=1, hit=True, shadow=88.0))

    decision = _build_summary(events, (1,))["ranking_decision"]

    assert decision["status"] == "candidate"
    assert decision["recommended_strategy"] == "candidate_shadow_then_score"
    assert decision["recommended_top_k"] == 1
    candidate = decision["candidates"]["candidate_shadow_then_score"]
    assert candidate["sample_ok"] is True
    assert candidate["lift_ok"] is True
    assert candidate["risk_ok"] is True
    assert candidate["hit_rate_delta_pct"] == 100.0


def test_policy_selection_uses_promoted_strategy_for_latest_candidates() -> None:
    events = []
    for day in range(20260501, 20260513):
        events.append(_event(day, f"A{day}", ai=False, score=0.99, count=2, hit=False, shadow=25.0))
        events.append(_event(day, f"B{day}", ai=False, score=0.30, count=1, hit=True, shadow=88.0, entry=82.0))
    events[-1]["entry_quality_risk_flags"] = ["短线涨幅偏快"]
    summary = _build_summary(events, (1,))

    selection = _policy_selection(events, summary["ranking_decision"])

    assert selection["uses_promoted_ranking"] is True
    assert selection["selection_strategy"] == "candidate_shadow_then_score"
    assert selection["recommend_date"] == 20260512
    assert [pick["code"] for pick in selection["picks"]] == ["B20260512"]
    assert selection["picks"][0]["action_status"] == "ready_for_ai_review"
    assert selection["picks"][0]["candidate_quality_score"] == 88.0
    assert selection["picks"][0]["risk_adjusted_quality_score"] == 83.0
    assert selection["picks"][0]["entry_risk_penalty"] == 5.0
    assert "短线涨幅偏快" in selection["picks"][0]["risk_factors"]
    assert selection["picks"][0]["next_step"] == "生成 AI 研报并结合持仓形成攻防决策"
    assert selection["action_plan"]["new_buy_allowed"] is False
    assert selection["action_plan"]["ai_review_allowed"] is True
    assert selection["action_plan"]["trade_readiness"] == "research_only"
    assert selection["action_plan"]["candidate_action"] == "generate_ai_report"
    assert selection["action_plan"]["next_tool"]["tool"] == "generate_ai_report"


def test_policy_selection_candidate_guard_blocks_unready_latest_pick() -> None:
    events = []
    for day in range(20260501, 20260513):
        events.append(_event(day, f"A{day}", ai=False, score=0.99, count=2, hit=False, shadow=25.0))
        events.append(_event(day, f"B{day}", ai=False, score=0.30, count=1, hit=True, shadow=88.0))
    events[-1]["label_ready"] = False
    events[-1]["label_status"] = "partial_window"
    summary = _build_summary(events, (1,))

    selection = _policy_selection(events, summary["ranking_decision"])
    guard = policy_candidate_guard_summary(selection)

    assert selection["picks"][0]["code"] == "B20260512"
    assert guard["direct_buy_blocked_count"] == 1
    assert guard["candidates"][0]["reason"] == "候选标签未成熟，禁止直接买入"


def test_policy_selection_candidate_guard_blocks_research_only_latest_pick() -> None:
    events = []
    for day in range(20260501, 20260513):
        events.append(_event(day, f"A{day}", ai=False, score=0.99, count=2, hit=False, shadow=25.0))
        events.append(_event(day, f"B{day}", ai=False, score=0.30, count=1, hit=True, shadow=88.0))
    summary = _build_summary(events, (1,))

    selection = _policy_selection(events, summary["ranking_decision"])
    guard = policy_candidate_guard_summary(selection)

    assert selection["picks"][0]["label_ready"] is True
    assert selection["action_plan"]["trade_readiness"] == "research_only"
    assert selection["action_plan"]["new_buy_allowed"] is False
    assert guard["direct_buy_blocked_count"] == 1
    assert guard["candidates"][0]["reason"] == "候选未开放新增买入，禁止直接买入"
    assert guard["candidates"][0]["trade_readiness"] == "research_only"
    assert guard["candidates"][0]["new_buy_allowed"] is False


def test_policy_selection_marks_unpromoted_pick_as_watch_only() -> None:
    events = [
        _event(20260515, "A", ai=False, score=0.99, count=2, hit=True),
        _event(20260515, "B", ai=False, score=0.30, count=1, hit=False),
    ]
    summary = _build_summary(events, (1,))

    selection = _policy_selection(events, summary["ranking_decision"])

    assert selection["uses_promoted_ranking"] is False
    assert selection["selection_strategy"] == "score_only"
    assert selection["picks"][0]["code"] == "A"
    assert selection["picks"][0]["action_status"] == "watch_only"
    assert "排序接入门槛未过，按 score_only 观察" in selection["picks"][0]["risk_factors"]
    assert selection["action_plan"]["new_buy_allowed"] is False
    assert selection["action_plan"]["ai_review_allowed"] is False
    assert selection["action_plan"]["candidate_action"] == "watch_only"
    assert selection["action_plan"]["review_status"] == "watch_only"
    assert "next_tool" not in selection["action_plan"]


def test_top_k_summary_can_rank_by_quality_grade_when_score_missing() -> None:
    events = [
        {**_event(20260515, "A", ai=False, score=0.99, count=3, hit=False), "candidate_shadow_grade": "D"},
        {**_event(20260515, "B", ai=False, score=0.30, count=1, hit=True), "candidate_shadow_grade": "S"},
    ]

    shadow_quality = _top_k_summary(events, 1, "candidate_shadow_then_score")

    assert shadow_quality["hit_rate_pct"] == 100.0


def test_event_summary_groups_candidate_quality_grades() -> None:
    events = [
        {**_event(20260515, "A", ai=False, score=0.99, count=3, hit=True), "candidate_shadow_grade": "S"},
        {**_event(20260515, "B", ai=False, score=0.80, count=1, hit=False), "candidate_shadow_grade": "D"},
        {**_event(20260516, "C", ai=True, score=0.70, count=1, hit=True), "entry_quality_grade": "A"},
        {**_event(20260516, "D", ai=False, score=0.50, count=2, hit=False), "entry_quality_grade": "C"},
    ]

    summary = _build_summary(events, (1, 3))

    assert summary["candidate_shadow_grade"]["S"]["hit_rate_pct"] == 100.0
    assert summary["candidate_shadow_grade"]["D"]["hit_rate_pct"] == 0.0
    assert summary["candidate_shadow_grade"]["unknown"]["rows_total"] == 2
    assert summary["entry_quality_grade"]["A"]["hit_rate_pct"] == 100.0
    assert summary["entry_quality_grade"]["C"]["hit_rate_pct"] == 0.0


def test_quality_feature_fields_merge_observation_and_row_features() -> None:
    observed = {
        "candidate_shadow_score": {"score": 88.456, "grade": "S"},
        "entry_quality": {"score": 76, "grade": "A", "risk_flags": ["缩量不足"]},
    }
    row = {
        "features_json": (
            '{"candidate_shadow_score":{"score":42,"grade":"D"},'
            '"entry_quality":{"score":"nan","grade":"Z","risk_flags":"追高延展"}}'
        )
    }

    fields = _quality_feature_fields(row, observed)

    assert fields["candidate_shadow_score"] == 42.0
    assert fields["candidate_shadow_grade"] == "D"
    assert fields["entry_quality_score"] is None
    assert fields["entry_quality_grade"] == "unknown"
    assert fields["entry_quality_risk_flags"] == ["追高延展"]


def test_observation_feature_map_merges_same_day_features() -> None:
    rows = [
        {
            "trade_date": "2026-05-15",
            "code": 1,
            "features_json": '{"candidate_shadow_score":{"score":82,"grade":"S"}}',
        },
        {
            "trade_date": "20260515",
            "code": "000001",
            "features_json": {"entry_quality": {"score": 72, "grade": "A"}},
        },
    ]

    features = _observation_feature_map(rows)[("000001", "20260515")]

    assert features["candidate_shadow_score"]["grade"] == "S"
    assert features["entry_quality"]["score"] == 72


def test_observation_feature_map_keeps_cross_market_code_shape() -> None:
    rows = [
        {
            "trade_date": "2026-05-15",
            "code": "00700",
            "features_json": '{"candidate_shadow_score":{"score":82,"grade":"S"}}',
        }
    ]

    features = _observation_feature_map(rows, market="hk")

    assert ("00700", "20260515") in features
    assert ("000700", "20260515") not in features


def _event(
    rec_date: int,
    code: str,
    *,
    ai: bool,
    score: float,
    count: int,
    hit: bool,
    shadow: float | None = None,
    entry: float | None = None,
) -> dict:
    event = {
        "recommend_date": rec_date,
        "code": code,
        "is_ai_recommended": ai,
        "funnel_score": score,
        "recommend_count": count,
        "label_ready": True,
        "hit_target": hit,
        "mfe_horizon_pct": 12.0 if hit else 4.0,
        "mae_horizon_pct": -3.0,
    }
    if shadow is not None:
        event["candidate_shadow_score"] = shadow
    if entry is not None:
        event["entry_quality_score"] = entry
    return event
