from core.candidate_report_semantics import (
    candidate_phase,
    candidate_role,
    candidate_semantic_parts,
    candidate_theme,
)


def test_candidate_semantics_use_persisted_mainline_metadata():
    reasons = '{"theme":"光模块","reasons":["板块共振"]}'

    assert candidate_theme(reasons) == "光模块"
    assert candidate_phase("强主线分歧") == "分歧机会"
    assert candidate_role(0.81, "mainline") == "主线核心"
    assert candidate_semantic_parts(
        candidate_reasons=reasons,
        candidate_status="强主线分歧",
        stock_role_score=0.81,
        candidate_lane="mainline",
    ) == ["光模块", "分歧机会", "主线核心"]


def test_candidate_semantics_degrade_cleanly_for_old_rows():
    assert candidate_theme("not-json") == ""
    assert candidate_phase("") == ""
    assert candidate_role(None, "mainline") == "主线候选"
    assert candidate_role(None, "trend") == ""
