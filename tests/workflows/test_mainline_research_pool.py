from __future__ import annotations

from types import SimpleNamespace

from core.candidate_policy import cap_quality_candidates
from core.funnel_selection import promote_l2_bypass_for_ai
from core.mainline_engine import mainline_candidate_entries
from core.wyckoff_engine import FunnelConfig
from workflows.funnel_render_context import _build_review_score_maps


def _candidates() -> list[dict]:
    return [
        {"code": f"{index:06d}", "status": "主线买点候选", "mainline_score": 0.9 - index * 0.01}
        for index in range(1, 7)
    ]


def test_live_candidate_pool_caps_mainline_rows_at_ai_promotion_cap(monkeypatch) -> None:
    """实盘候选池按 ``layers.mainline_ai_cap`` 卡名额。

    同一批 6 只候选跑两次:cap=3 出 3 行,cap=0(不限)出 6 行。留着不限的那次是为了
    区分「名额卡住了」和「候选本身只有 3 只」。
    """
    import workflows.funnel_candidates as mod

    for name in ("detect_markup_stage", "build_candidate_entries", "build_l1_candidate_lane_entries"):
        monkeypatch.setattr(mod, name, lambda *args, **kwargs: [])
    for name in ("detect_accum_stage", "layer5_exit_signals"):
        monkeypatch.setattr(mod, name, lambda *args, **kwargs: {})
    monkeypatch.setattr(mod, "annotate_trend_drawdown_risk", lambda *args: None)
    monkeypatch.setattr(mod, "rank_l3_candidates", lambda **kwargs: ([], {}))
    strategic = SimpleNamespace(markup_symbols=[], stage_map={}, pool=[])

    def _run(mainline_ai_cap: int):
        layers = SimpleNamespace(
            l1_passed=[],
            l2_passed=[],
            l3_passed=[],
            l2_channel_map={},
            triggers={},
            top_sectors=[],
            mainline_candidates=_candidates(),
            mainline_ai_cap=mainline_ai_cap,
            sector_rotation={},
        )
        return mod.build_candidate_outputs(
            layers=layers,
            strategic=strategic,
            all_df_map={},
            sector_map={},
            cfg=FunnelConfig(),
        )

    capped = _run(3)
    uncapped = _run(0)

    assert len(uncapped.mainline_candidate_entries) == 6
    assert len(uncapped.candidate_entries) == 6
    assert len(capped.mainline_candidate_entries) == 3
    assert len(capped.candidate_entries) == 3
    # 名额内留下的是分数最高的 3 只(_candidates 从 0.89 递减)。
    assert [item["code"] for item in capped.candidate_entries] == ["000001", "000002", "000003"]
    assert all(item["score"] > 0 for item in capped.candidate_entries)
    assert all(item["score"] > 0 for item in uncapped.candidate_entries)


def test_mainline_score_map_repairs_truncated_entries_without_lowering_existing_score() -> None:
    candidates = _candidates()
    truncated = mainline_candidate_entries(candidates, max_count=3)
    truncated[0]["score"] = 97.0
    reasons, keys, scores = _build_review_score_maps(
        review_triggers={},
        candidate_entry_map={item["code"]: item for item in truncated},
        strategic_l2_bypass_set=set(),
        strategic_l2_bypass_reason_map={},
        theme_badge_map={},
        theme_bonus_map={},
        capital_migration_badge_map={},
        capital_migration_bonus_map={},
        mainline_candidates=candidates,
    )

    assert len(scores) == 6
    assert scores["000001"] == 97.0
    assert scores["000006"] == 84.0
    assert keys["000006"] == ["mainline"]
    assert "主线综合分 84.00" in reasons["000006"][0]


def test_full_mainline_scores_keep_deterministic_final_and_sector_caps() -> None:
    entries = mainline_candidate_entries(list(reversed(_candidates())))
    scores = {item["code"]: item["score"] for item in entries}
    scores["000006"] = scores["000004"]
    selected: list[str] = []
    count = promote_l2_bypass_for_ai(
        selected,
        [],
        [],
        list(reversed(scores)),
        scores,
        {},
        {},
        enabled=True,
        cap=4,
        total_cap=4,
    )
    final, capped, sector_capped = cap_quality_candidates(
        list(reversed(scores)),
        scores,
        {"000001": "通信", "000002": "通信", "000003": "通信", "000004": "电子", "000006": "电子"},
        total_cap=3,
        max_per_sector=2,
    )

    assert count == 4
    assert selected == ["000001", "000002", "000003", "000004"]
    assert final == ["000001", "000002", "000004"]
    assert sector_capped == ["000003"]
    assert set(capped) == {"000005", "000006"}
