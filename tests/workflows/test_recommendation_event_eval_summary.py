from workflows.recommendation_event_eval_summary import recommendation_event_eval_result_summary


def test_result_summary_surfaces_context_coverage() -> None:
    result = {
        "summary": {
            "all": {"rows_ready": 492, "rows_total": 703, "hit_rate_pct": 18.5},
            "ranking_decision": {"status": "watch", "watch_strategy": "recommend_count"},
            "context_coverage": {
                "rows_total": 703,
                "rows_matched": 654,
                "coverage_pct": 93.03,
                "ready_rows_on_observed_dates": 492,
                "ready_rows_matched_on_observed_dates": 443,
                "ready_observed_date_coverage_pct": 90.04,
                "status_counts": {"matched_observation": 555, "tracking_fallback": 99},
            },
        }
    }

    summary = recommendation_event_eval_result_summary(result)

    assert "上下文覆盖: 654/703 (93.03%)" in summary
    assert "成熟=443/492 (90.04%)" in summary
    assert "observation=555, tracking_fallback=99" in summary
