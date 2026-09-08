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


def test_result_summary_distinguishes_event_net_return_from_cumulative_tracking() -> None:
    result = {
        "summary": {
            "all": {
                "rows_ready": 12,
                "rows_total": 15,
                "rows_unready": 3,
                "net_return_rows": 12,
                "avg_net_close_return_horizon_pct": 1.5,
            },
            "ranking_decision": {
                "status": "candidate",
                "recommended_strategy": "candidate_shadow_then_score",
                "recommended_top_k": 1,
            },
        }
    }

    summary = recommendation_event_eval_result_summary(result)

    assert "入场日记0，第H根后续日线收盘" in summary
    assert "不是首次入池累计涨幅" in summary
    assert "扣费平均收益=1.5% (有效样本=12)；未成熟/不可评估=3" in summary
    assert "样本内排序研究候选" in summary
    assert "仍需跨期组合验收" in summary
