from __future__ import annotations

from core.candidate_quality import (
    ai_review_quality_gate_reason,
    entry_quality_risk_flags,
    entry_quality_risk_penalty,
    risk_adjusted_quality_metrics,
    split_ai_review_candidates,
)


def test_risk_adjusted_quality_metrics_penalize_entry_risks() -> None:
    row = {
        "funnel_score": 89.5,
        "candidate_shadow_score": 92.0,
        "entry_quality_score": 84.0,
        "entry_quality_risk_flags": ["短线涨幅偏快", "供给压力未消化"],
    }

    assert risk_adjusted_quality_metrics(row) == {
        "candidate_quality_score": 92.0,
        "risk_adjusted_quality_score": 82.0,
        "entry_risk_penalty": 10.0,
    }


def test_entry_quality_risk_penalty_is_capped() -> None:
    row = {"candidate_shadow_score": 90.0, "entry_quality_risk_flags": ["a", "b", "c", "d", "e"]}

    assert entry_quality_risk_penalty(row) == 20.0
    assert risk_adjusted_quality_metrics(row)["risk_adjusted_quality_score"] == 70.0


def test_entry_quality_risk_flags_accept_scalar_text() -> None:
    assert entry_quality_risk_flags(" 短线涨幅偏快 ") == ["短线涨幅偏快"]
    assert entry_quality_risk_flags("") == []


def test_ai_review_quality_gate_reason_requires_explicit_quality_score() -> None:
    assert ai_review_quality_gate_reason({"funnel_score": 0.99}, "LOW") == ""
    assert ai_review_quality_gate_reason({"candidate_shadow_score": 65.0}, "LOW") == (
        "LOW 风险调整质量分 65.00 低于AI复核门槛 70.00"
    )
    assert ai_review_quality_gate_reason({"candidate_shadow_score": 70.0}, "OK") == ""


def test_split_ai_review_candidates_blocks_low_quality_selected_rows() -> None:
    rows = [
        {"code": "000001", "name": "强候选", "selected_for_report": True, "candidate_shadow_score": 88.0},
        {"code": "000002", "name": "弱候选", "selected_for_report": True, "candidate_shadow_score": 65.0},
        {"code": "000003", "name": "观察项", "selected_for_report": False, "candidate_shadow_score": 92.0},
    ]

    split = split_ai_review_candidates(rows)

    assert [row["code"] for row in split["report_candidates"]] == ["000001"]
    assert [row["code"] for row in split["watch_candidates"]] == ["000002", "000003"]
    assert split["quality_gate"]["blocked_count"] == 1
    assert split["quality_gate"]["candidates"][0]["reason"] == (
        "000002 弱候选 风险调整质量分 65.00 低于AI复核门槛 70.00"
    )
