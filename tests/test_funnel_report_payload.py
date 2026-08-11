from __future__ import annotations

from types import SimpleNamespace

from core.funnel_report import FunnelReportMaps
from core.recommendation_payload import build_recommendation_payload
from workflows.funnel_ai_selection import FunnelAiSelection
from workflows.funnel_report_payload import (
    display_score,
    display_score_map,
    funnel_run_details,
    legacy_symbol_rows,
    modern_symbol_rows,
    selection_source,
    stage_name,
)


def _ctx(**overrides):
    base = {
        "accum_stage_map": {"000001": "Accum_C"},
        "all_df_map": {"000001": "df"},
        "bypass_triggers": {},
        "candidate_entries": [],
        "candidate_entry_map": {},
        "code_to_total_score": {"000001": 3.5},
        "external_seed_triggers": {},
        "formal_hit_set": {"000001"},
        "formal_triggers": {},
        "leader_radar_rows": [],
        "leader_radar_symbols": set(),
        "l2_channel_map": {},
        "l2_bypass_set": set(),
        "markup_symbols": set(),
        "metrics": {"layer3_score_map": {}},
        "name_map": {"000001": "平安银行"},
        "review_triggers": {},
        "sector_map": {"000001": "银行"},
        "strategic_l2_bypass_set": set(),
        "strategic_l2_bypass_triggers": {},
        "code_to_reasons": {"000001": ["SOS"]},
        "theme_badge_map": {},
    }
    base.update(overrides)
    base.setdefault(
        "report_maps",
        FunnelReportMaps(
            name_map=base["name_map"],
            sector_map=base["sector_map"],
            sector_rotation_map={},
            exit_signals={},
            latest_close_map={"000001": 10.0},
            theme_candidate_map={},
            theme_bonus_map={},
            code_to_trigger_keys={"000001": ["sos"]},
            code_to_reasons=base["code_to_reasons"],
            theme_badge_map=base["theme_badge_map"],
            layer3_score_map=base["metrics"].get("layer3_score_map", {}) or {},
        ),
    )
    return SimpleNamespace(**base)


def _selection() -> FunnelAiSelection:
    return FunnelAiSelection(
        selected_for_ai=["000001"],
        trend_selected=["000001"],
        accum_selected=[],
        score_map={"000001": 2.0},
        ai_policy={"shadow_added": ["000001"]},
        theme_promoted_count=0,
    )


def test_funnel_payload_helpers_preserve_stage_source_and_score_priority():
    ctx = _ctx(candidate_entry_map={"000001": {"state": "Breakout"}})

    assert stage_name(ctx, "000001") == "Breakout"
    assert selection_source(ctx, "000001") == "alpha_candidate"
    assert display_score(ctx, _selection(), "000001") == 3.5


def test_modern_symbol_rows_use_display_score_as_priority_score():
    rows = modern_symbol_rows(_ctx(metrics={"layer3_score_map": {"000001": 0.82}}), _selection())

    assert rows[0]["score"] == 3.5
    assert rows[0]["priority_score"] == 3.5
    assert rows[0]["layer3_quality_score"] == 0.82


def test_symbol_rows_mark_degraded_run_as_observe_only():
    metrics = {
        "layer3_score_map": {},
        "data_quality": {"status": "degraded", "trade_readiness": "observe_only"},
    }

    rows = modern_symbol_rows(_ctx(metrics=metrics), _selection())

    assert rows[0]["trade_readiness"] == "observe_only"
    assert rows[0]["data_quality_status"] == "degraded"


def test_legacy_symbol_rows_infer_candidate_track_from_entry_type():
    ctx = _ctx(candidate_entry_map={"000001": {"entry_type": "spring", "score": 80.0}})

    rows = legacy_symbol_rows(ctx, _selection())

    assert rows[0]["track"] == "Accum"


def test_display_score_keeps_stronger_selection_score():
    selection = FunnelAiSelection(
        selected_for_ai=["000001"],
        trend_selected=["000001"],
        accum_selected=[],
        score_map={"000001": 9.0},
        ai_policy={},
        theme_promoted_count=0,
    )

    assert display_score(_ctx(), selection, "000001") == 9.0


def test_display_score_map_uses_trigger_priority_for_selected_codes():
    got = display_score_map(_ctx(), _selection())

    assert got["000001"] == 3.5


def test_display_score_map_treats_invalid_scores_as_zero():
    selection = FunnelAiSelection(
        selected_for_ai=["000001"],
        trend_selected=["000001"],
        accum_selected=[],
        score_map={"000001": "bad", "000002": float("inf")},
        ai_policy={},
        theme_promoted_count=0,
    )
    ctx = _ctx(code_to_total_score={"000001": float("nan")})

    assert display_score(ctx, selection, "000001") == 0.0
    assert display_score_map(ctx, selection) == {"000001": 0.0, "000002": 0.0}


def test_funnel_run_details_keeps_report_payload_fields():
    details = funnel_run_details(_ctx(), _selection(), content="内容", title="标题", symbols=[{"code": "000001"}])

    assert details["content"] == "内容"
    assert details["title"] == "标题"
    assert details["symbols_for_report"] == [{"code": "000001"}]
    assert details["selected_for_ai"] == ["000001"]
    assert details["shadow_added"] == ["000001"]
    assert details["name_map"] == {"000001": "平安银行"}
    assert details["priority_score_map"] == {"000001": 3.5}


def test_funnel_run_details_overrides_trade_mode_when_data_quality_is_degraded():
    metrics = {
        "layer3_score_map": {},
        "data_quality": {"status": "degraded", "trade_readiness": "observe_only"},
    }

    details = funnel_run_details(_ctx(metrics=metrics), _selection(), content="内容", title="标题", symbols=[])

    assert details["trade_mode"]["mode"] == "observe_only"
    assert details["trade_mode"]["allow_ai_review"] is True
    assert details["trade_mode"]["allow_recommendation_write"] is False


def test_funnel_run_details_carries_strategy_policy_evidence():
    selection = FunnelAiSelection(
        selected_for_ai=["000001"],
        trend_selected=["000001"],
        accum_selected=[],
        score_map={"000001": 2.0},
        ai_policy={
            "_dynamic_mode": "shadow",
            "_signal_weights": {"lps": 0.5},
            "_attribution_signal_weights": {"lps": 0.5},
            "_attribution_policy_meta": {
                "execution_policy": "shadow",
                "formal_dynamic_allowed": False,
                "next_action": "manual_review_dynamic_on",
                "policy_weight_active_scope": "尾盘+漏斗shadow",
                "selection_action_count": 1,
                "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
            },
        },
        theme_promoted_count=0,
    )

    details = funnel_run_details(_ctx(), selection, content="内容", title="标题", symbols=[])

    assert details["strategy_policy"] == {
        "dynamic_mode": "shadow",
        "signal_weights": {"lps": 0.5},
        "attribution_signal_weights": {"lps": 0.5},
        "attribution_policy_meta": {
            "execution_policy": "shadow",
            "formal_dynamic_allowed": False,
            "next_action": "manual_review_dynamic_on",
            "policy_weight_active_scope": "尾盘+漏斗shadow",
            "selection_action_count": 1,
            "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
        },
        "selection_action_count": 1,
        "selection_action_summary": "候选源治理 1 项：candidate_lane=trend_pullback 降级到 shadow/人工复核×0.75",
        "formal_dynamic_allowed": False,
        "policy_weight_active_scope": "尾盘+漏斗shadow",
        "execution_policy": "shadow",
        "next_action": "manual_review_dynamic_on",
    }


def test_recommendation_payload_keeps_capital_migration_bonus():
    payload = build_recommendation_payload(
        20260630,
        [
            {
                "code": "000001",
                "name": "平安银行",
                "tag": "SOS、资金迁入",
                "priority_score": 14.5,
                "capital_migration_bonus": 4.5,
            }
        ],
        {},
        {},
    )

    assert payload[0]["capital_migration_bonus"] == 4.5


def test_recommendation_payload_sticks_first_price_across_days():
    payload = build_recommendation_payload(
        20260702,
        [{"code": "000001", "name": "平安银行", "initial_price": 12.0}],
        {1: 1},
        {1: {20260630}},
        {1: 10.5},
    )

    assert payload[0]["initial_price"] == 10.5
    assert payload[0]["current_price"] == 12.0
    assert payload[0]["recommend_count"] == 2
