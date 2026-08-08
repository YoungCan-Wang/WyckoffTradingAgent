from __future__ import annotations

import pytest

from workflows.step4_pipeline import (
    _step4_candidate_meta,
    is_confirmed_step4_candidate,
    step4_ai_candidate_policy,
)


@pytest.mark.parametrize(
    "item",
    [
        {"status": "unconfirmed"},
        {"signal_status": "pending", "tag": "SOS(确认)"},
        {"confirm_status": "未确认，仅观察"},
        {"candidate_status": "待确认", "recommend_reason": "confirmed"},
        {"tag": "二次确认观察"},
        {"selection_source": "signal_confirmed", "candidate_status": "市场拦截观察"},
    ],
)
def test_step4_confirmation_rejects_negative_or_observation_states(item):
    assert not is_confirmed_step4_candidate(item)


@pytest.mark.parametrize(
    "item",
    [
        {"status": "confirmed"},
        {"status": "confirmed", "source_type": "signal_pending"},
        {"is_confirmed": True},
        {"selection_source": "signal_confirmed"},
        {"selection_source": "跨日确认"},
        {"tag": "SOS(确认)"},
        {"tag": "EVR(二次确认)"},
        {"tag": "SPRING(跨日确认)"},
        {"recommend_reason": "LPS二次确认(A+C)"},
        {"recommend_reason": "LPS跨日确认(A+C)"},
        {"tag": "主线买点确认 | 威科夫候选"},
    ],
)
def test_step4_confirmation_accepts_explicit_confirmed_states(item):
    assert is_confirmed_step4_candidate(item)


def test_step4_candidate_meta_veto_only_uses_rules_and_ai_invalidations(monkeypatch):
    monkeypatch.setenv("STEP4_AI_CANDIDATE_POLICY", "veto_only")
    symbols_info = [
        {"code": "000001", "confirm_status": "confirmed", "priority_score": 95},
        {"code": "000002", "confirm_status": "confirmed", "priority_score": 90},
        {"code": "000003", "confirm_status": "confirmed", "new_buy_allowed": False},
        {"code": "000004", "signal_status": "pending", "priority_score": 99},
    ]
    report = "## 💀 逻辑破产 (Invalidated)\n- 000002 放量失守\n\n## ⏳ 储备营地\n- 000001"

    selected, blocked = _step4_candidate_meta(symbols_info, ["000004"], report)

    assert [item["code"] for item in selected] == ["000001"]
    assert blocked == 2


def test_step4_candidate_meta_invalid_policy_falls_back_to_shadow(monkeypatch):
    """非法取值回退到 shadow：LLM 无拦截权是更安全的默认，配错不应静默恢复否决权。"""
    monkeypatch.setenv("STEP4_AI_CANDIDATE_POLICY", "legacy")
    symbols_info = [
        {"code": "000001", "confirm_status": "confirmed"},
        {"code": "000002", "confirm_status": "confirmed"},
    ]
    report = "## 💀 逻辑破产\n- 000002 放量失守"

    selected, blocked = _step4_candidate_meta(symbols_info, [], report)

    assert [item["code"] for item in selected] == ["000001", "000002"]
    assert blocked == 0


def test_step4_ai_candidate_policy_defaults_to_shadow(monkeypatch):
    monkeypatch.delenv("STEP4_AI_CANDIDATE_POLICY", raising=False)

    assert step4_ai_candidate_policy() == "shadow"


def test_step4_candidate_meta_shadow_ignores_ai_classification(monkeypatch):
    monkeypatch.setenv("STEP4_AI_CANDIDATE_POLICY", "shadow")
    symbols_info = [
        {"code": "000001", "confirm_status": "confirmed"},
        {"code": "000002", "confirm_status": "confirmed"},
    ]
    report = "## 💀 逻辑破产\n- 000002 放量失守"

    selected, blocked = _step4_candidate_meta(symbols_info, [], report)

    assert [item["code"] for item in selected] == ["000001", "000002"]
    assert blocked == 0


def test_step4_candidate_meta_deduplicates_codes(monkeypatch):
    monkeypatch.setenv("STEP4_AI_CANDIDATE_POLICY", "shadow")
    symbols_info = [
        {"code": "000001", "confirm_status": "confirmed", "priority_score": 95},
        {"code": "000001", "confirm_status": "confirmed", "priority_score": 90},
    ]

    selected, blocked = _step4_candidate_meta(symbols_info, [], "")

    assert [item["code"] for item in selected] == ["000001"]
    assert blocked == 0
