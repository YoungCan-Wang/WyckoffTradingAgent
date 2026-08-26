from __future__ import annotations

import pytest

from agents.research_tools import research_hypothesis
from integrations import local_db


@pytest.fixture(autouse=True)
def research_db(tmp_path, monkeypatch):
    local_db.reset_connection()
    monkeypatch.setattr("core.constants.LOCAL_DB_PATH", tmp_path / "research.db")
    local_db.init_db()
    yield
    local_db.reset_connection()


def _create() -> dict:
    result = research_hypothesis(
        action="create",
        title="LPS 量能确认",
        thesis="LPS 经量能放大确认后，未来十日收益更稳定",
        universe="cn_a",
        signal_definition="LPS + vol_ratio_confirmation >= 70",
        invalidation_criteria="样本外十日均值收益连续三期为负",
    )
    assert result["status"] == "created"
    return result["hypothesis"]


def test_create_update_and_list_hypothesis():
    hypothesis = _create()

    updated = research_hypothesis(
        action="transition",
        hypothesis_id=hypothesis["hypothesis_id"],
        target_status="testing",
    )
    listed = research_hypothesis(action="list", status="testing")

    assert updated["hypothesis"]["status"] == "testing"
    assert listed["count"] == 1
    assert listed["hypotheses"][0]["title"] == "LPS 量能确认"


def test_validation_requires_backtest_and_stability_evidence():
    hypothesis = _create()
    hypothesis_id = hypothesis["hypothesis_id"]
    research_hypothesis(action="transition", hypothesis_id=hypothesis_id, target_status="testing")

    blocked = research_hypothesis(action="transition", hypothesis_id=hypothesis_id, target_status="validated")
    assert blocked["status"] == "blocked"
    assert {item["key"] for item in blocked["checklist"] if item["status"] == "blocked"} == {
        "backtest",
        "stability",
    }

    for evidence_type in ("backtest", "stability"):
        research_hypothesis(
            action="link_evidence",
            hypothesis_id=hypothesis_id,
            evidence_type=evidence_type,
            artifact_ref=f"artifact://{evidence_type}/1",
            verdict="pass",
            summary=f"{evidence_type} passed",
        )

    evaluated = research_hypothesis(action="evaluate", hypothesis_id=hypothesis_id)
    transitioned = research_hypothesis(
        action="transition",
        hypothesis_id=hypothesis_id,
        target_status="validated",
        reason="跨周期与相邻参数均通过",
    )
    assert evaluated["recommended_transition"] == "validated"
    assert transitioned["status"] == "transitioned"
    assert transitioned["hypothesis"]["status"] == "validated"
    assert transitioned["hypothesis"]["transitions"][0]["reason"] == "跨周期与相邻参数均通过"


def test_update_cannot_bypass_transition_gate_and_rejection_needs_reason():
    hypothesis = _create()
    hypothesis_id = hypothesis["hypothesis_id"]

    bypass = research_hypothesis(action="update", hypothesis_id=hypothesis_id, status="validated")
    rejected = research_hypothesis(action="transition", hypothesis_id=hypothesis_id, target_status="rejected")
    accepted = research_hypothesis(
        action="transition",
        hypothesis_id=hypothesis_id,
        target_status="rejected",
        reason="失效条件已触发",
    )

    assert "不允许修改 status" in bypass["error"]
    assert rejected["status"] == "blocked"
    assert accepted["hypothesis"]["status"] == "rejected"


def test_link_evidence_is_idempotent_and_keeps_structured_metrics():
    hypothesis = _create()
    hypothesis_id = hypothesis["hypothesis_id"]

    research_hypothesis(
        action="link_evidence",
        hypothesis_id=hypothesis_id,
        evidence_type="backtest",
        artifact_ref="actions://backtest-grid/123/backtest_confirmation.json",
        verdict="review",
        summary="首轮多周期结果",
        metrics={"weak_periods": 1},
    )
    result = research_hypothesis(
        action="link_evidence",
        hypothesis_id=hypothesis_id,
        evidence_type="backtest",
        artifact_ref="actions://backtest-grid/123/backtest_confirmation.json",
        verdict="pass",
        summary="复核通过",
        metrics={"weak_periods": 0},
    )

    evidence = result["hypothesis"]["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["verdict"] == "pass"
    assert evidence[0]["metrics"] == {"weak_periods": 0}


def test_rejects_unknown_status_and_missing_hypothesis():
    bad_status = research_hypothesis(action="list", status="live")
    missing = research_hypothesis(
        action="link_evidence",
        hypothesis_id="hyp_missing",
        evidence_type="backtest",
        artifact_ref="run://missing",
    )

    assert "status 必须是" in bad_status["error"]
    assert "研究假设不存在" in missing["error"]
