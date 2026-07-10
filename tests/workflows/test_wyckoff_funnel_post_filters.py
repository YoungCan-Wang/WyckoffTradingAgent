from __future__ import annotations

from types import SimpleNamespace

from workflows.wyckoff_funnel import _apply_ai_post_filters


def test_post_filters_sync_effective_score_before_min_score_and_rerank() -> None:
    ctx = SimpleNamespace(
        regime="NEUTRAL",
        code_to_trigger_keys={},
        code_to_total_score={"HIGH": 20.0, "LOW": 5.0},
        l2_channel_map={},
        all_df_map={},
        metrics={"min_funnel_score": 10.0},
    )
    score_map = {"HIGH": 1.0, "LOW": 15.0}

    selected, trend, accum = _apply_ai_post_filters(ctx, ["LOW", "HIGH"], ["LOW", "HIGH"], [], score_map, {})

    assert selected == ["HIGH", "LOW"]
    assert trend == ["HIGH", "LOW"]
    assert accum == []
    assert score_map["HIGH"] == 20.0


def test_post_filters_treat_invalid_scores_as_zero() -> None:
    ctx = SimpleNamespace(
        regime="NEUTRAL",
        code_to_trigger_keys={},
        code_to_total_score={"GOOD": 2.0, "BAD": "bad", "NAN": float("nan"), "INF": float("inf")},
        l2_channel_map={},
        all_df_map={},
        metrics={"min_funnel_score": 1.0},
    )
    score_map = {"GOOD": "bad", "BAD": "bad", "NAN": float("nan"), "INF": float("inf")}

    selected, trend, accum = _apply_ai_post_filters(
        ctx,
        ["BAD", "GOOD", "NAN", "INF"],
        ["BAD", "GOOD", "NAN", "INF"],
        [],
        score_map,
        {},
    )

    assert selected == ["GOOD"]
    assert trend == ["GOOD"]
    assert accum == []
    assert score_map == {"GOOD": 2.0, "BAD": 0.0, "NAN": 0.0, "INF": 0.0}
