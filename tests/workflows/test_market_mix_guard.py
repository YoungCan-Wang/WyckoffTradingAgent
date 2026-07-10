from __future__ import annotations

from types import SimpleNamespace

from workflows import wyckoff_funnel as workflow


def test_market_mix_guard_adds_main_or_chinext_when_selected_is_star_bse(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MIN_SCORE", 68.0)
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MAX_ADD", 2)
    ctx = SimpleNamespace(
        candidate_entries=[
            {"code": "000001", "score": 69.0, "risk": ""},
            {"code": "300001", "score": 75.0, "risk": "长上影"},
            {"code": "002001", "score": 70.0, "risk": ""},
        ],
        code_to_total_score={"000001": 69.0, "002001": 70.0},
    )
    ai_policy: dict = {}

    selected, trend, accum = workflow._apply_market_mix_guard(
        ctx,
        ["688001", "830000"],
        ["688001", "830000"],
        [],
        {},
        ai_policy,
    )

    assert selected == ["688001", "830000", "002001", "000001"]
    assert trend == selected
    assert accum == []
    assert ai_policy["market_mix_guard_added"] == ["002001", "000001"]


def test_market_mix_guard_keeps_accum_track_for_accum_candidate(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MIN_SCORE", 68.0)
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MAX_ADD", 2)
    ctx = SimpleNamespace(
        candidate_entries=[{"code": "000001", "score": 72.0, "risk": "", "track": "lps"}],
        code_to_total_score={},
    )
    score_map: dict[str, float] = {}

    selected, trend, accum = workflow._apply_market_mix_guard(
        ctx,
        ["688001"],
        ["688001"],
        [],
        score_map,
        {},
    )

    assert selected == ["688001", "000001"]
    assert trend == ["688001"]
    assert accum == ["000001"]
    assert score_map["000001"] == 72.0


def test_market_mix_guard_replaces_weak_star_candidate_when_total_cap_is_full(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MIN_SCORE", 68.0)
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MAX_ADD", 2)
    ctx = SimpleNamespace(
        candidate_entries=[{"code": "000001", "score": 76.0, "risk": ""}],
        code_to_total_score={"000001": 76.0},
    )
    ai_policy: dict = {"total_cap": 2}
    score_map = {"688001": 72.0, "830000": 80.0}

    selected, trend, accum = workflow._apply_market_mix_guard(
        ctx,
        ["688001", "830000"],
        ["688001", "830000"],
        [],
        score_map,
        ai_policy,
    )

    assert selected == ["830000", "000001"]
    assert trend == ["830000", "000001"]
    assert accum == []
    assert ai_policy["market_mix_guard_added"] == ["000001"]
    assert ai_policy["market_mix_guard_replaced"] == [{"removed": "688001", "added": "000001"}]


def test_market_mix_guard_does_not_exceed_total_cap_for_weaker_main_candidate(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MIN_SCORE", 68.0)
    ctx = SimpleNamespace(
        candidate_entries=[{"code": "000001", "score": 70.0, "risk": ""}],
        code_to_total_score={"000001": 70.0},
    )
    ai_policy: dict = {"total_cap": 1}
    score_map = {"688001": 75.0}

    selected, trend, accum = workflow._apply_market_mix_guard(
        ctx,
        ["688001"],
        ["688001"],
        [],
        score_map,
        ai_policy,
    )

    assert selected == ["688001"]
    assert trend == ["688001"]
    assert accum == []
    assert "market_mix_guard_added" not in ai_policy
    assert "已达上限" in ai_policy["market_mix_guard_reason"]
    assert "未强于现有科创/北交候选" in ai_policy["market_mix_guard_reason"]
    assert "低于市场均衡补入门槛" not in ai_policy["market_mix_guard_reason"]


def test_market_mix_guard_records_reason_when_no_main_or_chinext_candidate(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "FUNNEL_MARKET_MIX_MIN_SCORE", 68.0)
    ctx = SimpleNamespace(
        candidate_entries=[
            {"code": "000001", "score": 66.0, "risk": ""},
            {"code": "300001", "score": 80.0, "risk": "短线过热"},
        ],
        code_to_total_score={"000001": 66.0},
    )
    ai_policy: dict = {}

    selected, _trend, _accum = workflow._apply_market_mix_guard(ctx, ["688001"], ["688001"], [], {}, ai_policy)

    assert selected == ["688001"]
    assert "低于市场均衡补入门槛" in ai_policy["market_mix_guard_reason"]
