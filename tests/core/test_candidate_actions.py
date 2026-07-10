from core.candidate_actions import (
    candidate_action_fields,
    candidate_action_role,
)


def test_ready_for_ai_review_requires_explicit_buy_permission() -> None:
    fields = candidate_action_fields({"action_status": "ready_for_ai_review"})

    assert fields == {
        "action_status": "ready_for_ai_review",
        "action_label": "可进入AI复核",
        "action_level": "ai_review",
        "direct_buy_allowed": False,
    }


def test_ready_for_ai_review_can_allow_direct_buy_when_explicitly_open() -> None:
    fields = candidate_action_fields(
        {
            "action_status": "ready_for_ai_review",
            "new_buy_allowed": True,
            "trade_readiness": "review_ready",
        }
    )

    assert fields["direct_buy_allowed"] is True


def test_blocked_status_keeps_human_label_and_blocks_buy() -> None:
    fields = candidate_action_fields(
        {
            "action_status": "blocked_by_market_gate",
            "new_buy_allowed": True,
            "trade_readiness": "review_ready",
        }
    )

    assert fields["action_label"] == "风险闸门关闭"
    assert fields["action_level"] == "blocked"
    assert fields["direct_buy_allowed"] is False


def test_action_role_keeps_ready_candidate_boundaries() -> None:
    assert candidate_action_role("ready_for_ai_review") == "首选"
    assert candidate_action_role("ready_for_ai_review", ready_rank=2) == "备选复核候选"
    assert candidate_action_role("ready_for_ai_review", guard_reason="只能复核") == "受限复核候选"
    assert candidate_action_role("watch_only") == "观察候选"
