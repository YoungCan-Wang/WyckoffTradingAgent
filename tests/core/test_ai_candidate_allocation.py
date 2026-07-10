"""AI candidate allocation policy tests."""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from core.ai_candidate_allocation import (
    DEFAULT_AI_QUOTA_BY_FAMILY,
    AiCandidateAllocationConfig,
    _track_alignment_bonus,
    allocate_ai_candidates,
    resolve_ai_candidate_policy,
)
from core.candidate_policy import CandidatePolicyConfig
from core.wyckoff_engine import FunnelResult
from workflows.ai_candidate_allocation_config import ai_candidate_allocation_config_from_env
from workflows.candidate_policy_config import candidate_policy_config_from_env

ROOT = Path(__file__).resolve().parents[2]


class TestAllocateAiCandidates:
    def test_bear_rebound_uses_defensive_quota_family(self):
        config = AiCandidateAllocationConfig(
            total_cap=8,
            quota_by_family={"BEAR_REBOUND": (1, 2)},
        )

        policy = resolve_ai_candidate_policy("BEAR_REBOUND", config=config)

        assert policy["quota_family"] == "BEAR_REBOUND"
        assert policy["trend_quota"] == 1
        assert policy["accum_quota"] == 2

    def test_bear_rebound_default_quota_blocks_ai_candidates(self):
        policy = resolve_ai_candidate_policy("BEAR_REBOUND")

        assert policy["quota_family"] == "BEAR_REBOUND"
        assert policy["trend_quota"] == 0
        assert policy["accum_quota"] == 0

    def test_risk_on_default_quota_keeps_research_candidates(self, monkeypatch):
        monkeypatch.delenv("FUNNEL_AI_RISK_ON_TREND", raising=False)
        monkeypatch.delenv("FUNNEL_AI_RISK_ON_ACCUM", raising=False)

        config = ai_candidate_allocation_config_from_env()
        policy = resolve_ai_candidate_policy("RISK_ON", config=config)

        assert policy["requested_trend_quota"] == 5
        assert policy["requested_accum_quota"] == 1
        assert policy["trend_quota"] == 5
        assert policy["accum_quota"] == 1

    def test_neutral_default_quota_is_trend_dominant(self, monkeypatch):
        monkeypatch.delenv("FUNNEL_AI_NEUTRAL_TREND", raising=False)
        monkeypatch.delenv("FUNNEL_AI_NEUTRAL_ACCUM", raising=False)

        config = ai_candidate_allocation_config_from_env()
        policy = resolve_ai_candidate_policy("NEUTRAL", config=config)

        assert policy["requested_trend_quota"] == 5
        assert policy["requested_accum_quota"] == 1
        assert policy["trend_quota"] == 5
        assert policy["accum_quota"] == 1

    def test_allocation_env_loader_stays_in_workflow_layer(self, monkeypatch):
        monkeypatch.setenv("FUNNEL_AI_TOTAL_CAP", "6")
        monkeypatch.setenv("FUNNEL_AI_RISK_ON_TREND", "4")
        monkeypatch.setenv("FUNNEL_AI_RISK_ON_ACCUM", "2")

        config = ai_candidate_allocation_config_from_env()
        policy = resolve_ai_candidate_policy("RISK_ON", config=config)

        assert policy["total_cap"] == 6
        assert policy["trend_quota"] == 4
        assert policy["accum_quota"] == 2

    def test_candidate_policy_default_risk_on_overheat_matches_production(self, monkeypatch):
        monkeypatch.delenv("FUNNEL_LOSS_GUARD_RISK_ON_PRE5_RET", raising=False)

        config = candidate_policy_config_from_env()

        assert config.risk_on_pre5_ret == 35.0

    def test_candidate_policy_env_can_override_risk_on_overheat(self, monkeypatch):
        monkeypatch.setenv("FUNNEL_LOSS_GUARD_RISK_ON_PRE5_RET", "28")

        config = candidate_policy_config_from_env()

        assert config.risk_on_pre5_ret == 28.0

    def test_production_workflows_stay_aligned_with_core_policy_defaults(self):
        default_allocation = AiCandidateAllocationConfig()
        default_policy = CandidatePolicyConfig()
        for path, job_name in (
            (".github/workflows/wyckoff_funnel.yml", "run"),
            (".github/workflows/backtest_grid.yml", "grid"),
        ):
            env = _workflow_job_env(path, job_name)

            assert int(env["FUNNEL_AI_TOTAL_CAP"]) == default_allocation.total_cap
            for family, (trend_quota, accum_quota) in DEFAULT_AI_QUOTA_BY_FAMILY.items():
                assert int(env[f"FUNNEL_AI_{family}_TREND"]) == trend_quota
                assert int(env[f"FUNNEL_AI_{family}_ACCUM"]) == accum_quota
            assert float(env["FUNNEL_LOSS_GUARD_RISK_ON_PRE5_RET"]) == default_policy.risk_on_pre5_ret
            assert "tradeable_l4" in str(env["FUNNEL_AI_SELECTION_MODE"])
            assert "shadow" in str(env["FUNNEL_DYNAMIC_POLICY"])
            assert int(env["FUNNEL_DYNAMIC_POLICY_HORIZON"]) == 5
            assert int(env["STRATEGY_ATTRIBUTION_MAX_AGE_DAYS"]) == 7

    def test_evr_and_compression_only_hits_enter_quota_tracks(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002"],
            layer2_symbols=["000001", "000002"],
            layer3_symbols=["000001", "000002"],
            top_sectors=[],
            triggers={"evr": [("000001", 2.0)], "compression": [("000002", 0.4)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 2,
                "trend_quota": 1,
                "accum_quota": 1,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == ["000001"]
        assert accum == ["000002"]
        assert scores["000001"] > 0
        assert scores["000002"] > 0

    def test_sos_outranks_evr_after_downweight_iteration(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002"],
            layer2_symbols=["000001", "000002"],
            layer3_symbols=["000001", "000002"],
            top_sectors=[],
            triggers={"sos": [("000001", 4.0)], "evr": [("000002", 4.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "趋势延续"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        _trend, _accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 2,
                "trend_quota": 2,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert scores["000001"] > scores["000002"]

    def test_regime_scoped_weight_changes_ai_candidate_scores(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002"],
            layer2_symbols=["000001", "000002"],
            layer3_symbols=["000001", "000002"],
            top_sectors=[],
            triggers={"sos": [("000001", 4.0)], "evr": [("000002", 4.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "趋势延续"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        _trend, _accum, scores = allocate_ai_candidates(
            result,
            [],
            "RISK_ON",
            policy_override={
                "total_cap": 2,
                "trend_quota": 2,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
            signal_weight_map={"sos|regime=RISK_ON": 0.4},
        )

        assert scores["000002"] > scores["000001"]

    def test_layer3_score_breaks_same_trigger_priority_ties(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002"],
            layer2_symbols=["000001", "000002"],
            layer3_symbols=["000001", "000002"],
            top_sectors=[],
            triggers={"sos": [("000002", 5.0), ("000001", 5.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "点火破局"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
            layer3_score_map={"000001": 1.0, "000002": 0.2},
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            ["000001", "000002"],
            "NEUTRAL",
            policy_override={
                "total_cap": 2,
                "trend_quota": 2,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == ["000001", "000002"]
        assert accum == []
        assert scores["000001"] > scores["000002"]

    def test_layer3_score_ignores_non_finite_values(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002"],
            layer2_symbols=["000001", "000002"],
            layer3_symbols=["000001", "000002"],
            top_sectors=[],
            triggers={"sos": [("000001", 5.0), ("000002", 5.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "点火破局"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
            layer3_score_map={"000001": float("nan"), "000002": 0.5},
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            ["000001", "000002"],
            "NEUTRAL",
            policy_override={
                "total_cap": 2,
                "trend_quota": 2,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == ["000002", "000001"]
        assert accum == []
        assert all(math.isfinite(score) for score in scores.values())
        assert scores["000002"] > scores["000001"]

    def test_score_map_keeps_best_score_when_candidate_sources_overlap(self):
        result = FunnelResult(
            layer1_symbols=["000001"],
            layer2_symbols=["000001"],
            layer3_symbols=["000001"],
            top_sectors=[],
            triggers={},
            stage_map={},
            markup_symbols=["000001"],
            exit_signals={},
            channel_map={"000001": "点火破局"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
            candidate_entries=[{"code": "000001", "score": 1.0, "track": "trend", "entry_type": "sos"}],
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 1,
                "trend_quota": 1,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == ["000001"]
        assert accum == []
        assert scores["000001"] > 1.0

    def test_cross_track_duplicate_uses_stronger_track_assignment(self):
        result = FunnelResult(
            layer1_symbols=["000001"],
            layer2_symbols=["000001"],
            layer3_symbols=["000001"],
            top_sectors=[],
            triggers={"evr": [("000001", 1.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={},
            leader_radar_symbols=[],
            leader_radar_rows=[],
            candidate_entries=[
                {"code": "000001", "score": 80.0, "track": "accumulation", "entry_type": "spring"},
            ],
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 1,
                "trend_quota": 1,
                "accum_quota": 1,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == []
        assert accum == ["000001"]
        assert scores["000001"] == 80.0

    def test_candidate_entry_accum_track_alias_enters_accum_quota(self):
        result = FunnelResult(
            layer1_symbols=["000001"],
            layer2_symbols=["000001"],
            layer3_symbols=["000001"],
            top_sectors=[],
            triggers={},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={},
            leader_radar_symbols=[],
            leader_radar_rows=[],
            candidate_entries=[
                {"code": "000001", "score": 80.0, "track": "Accum", "entry_type": "spring"},
            ],
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 1,
                "trend_quota": 1,
                "accum_quota": 1,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == []
        assert accum == ["000001"]
        assert scores["000001"] == 80.0

    def test_candidate_entry_infers_accum_quota_from_entry_type_when_track_missing(self):
        result = FunnelResult(
            layer1_symbols=["000001"],
            layer2_symbols=["000001"],
            layer3_symbols=["000001"],
            top_sectors=[],
            triggers={},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={},
            leader_radar_symbols=[],
            leader_radar_rows=[],
            candidate_entries=[
                {"code": "000001", "score": 80.0, "entry_type": "lps"},
            ],
        )

        trend, accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 1,
                "trend_quota": 1,
                "accum_quota": 1,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == []
        assert accum == ["000001"]
        assert scores["000001"] == 80.0

    def test_sector_cap_skips_blocked_candidate_and_continues_filling_quota(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002", "000003"],
            layer2_symbols=["000001", "000002", "000003"],
            layer3_symbols=["000001", "000002", "000003"],
            top_sectors=[],
            triggers={"sos": [("000001", 9.0), ("000002", 8.0), ("000003", 7.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "点火破局", "000003": "点火破局"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        trend, accum, _scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            sector_map={"000001": "银行", "000002": "银行", "000003": "通信"},
            max_per_sector=1,
            policy_override={
                "total_cap": 3,
                "trend_quota": 2,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == ["000001", "000003"]
        assert accum == []

    def test_available_track_backfills_when_other_track_has_no_candidates(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002", "000003"],
            layer2_symbols=["000001", "000002", "000003"],
            layer3_symbols=["000001", "000002", "000003"],
            top_sectors=[],
            triggers={"sos": [("000001", 9.0), ("000002", 8.0), ("000003", 7.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "点火破局", "000003": "点火破局"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        trend, accum, _scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 3,
                "trend_quota": 1,
                "accum_quota": 2,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == ["000001", "000002", "000003"]
        assert accum == []

    def test_zero_quota_track_stays_disabled_during_backfill(self):
        result = FunnelResult(
            layer1_symbols=["000001", "000002"],
            layer2_symbols=["000001", "000002"],
            layer3_symbols=["000001", "000002"],
            top_sectors=[],
            triggers={"sos": [("000001", 9.0), ("000002", 8.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"000001": "点火破局", "000002": "点火破局"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        trend, accum, _scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 2,
                "trend_quota": 0,
                "accum_quota": 2,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
        )

        assert trend == []
        assert accum == []


class TestTrackAlignmentBonus:
    """一个候选被降权信号命中时，赛道对齐奖励必须按该候选实际命中的信号取权重，
    不能被同赛道里另一个健康信号的全局权重掩盖（否则精细降权会被稀释成粗粒度）。
    """

    def test_bonus_uses_hit_signal_weight_not_global_track_max(self):
        weights = {"sos": 1.0, "evr": 0.4, "trend_pullback": 1.0}
        hit_sets = {
            "sos": set(),
            "evr": {"X001"},
            "trend_pullback": set(),
            "spring": set(),
            "lps": set(),
            "compression": set(),
        }

        bonus = _track_alignment_bonus("X001", True, hit_sets, weights, "NEUTRAL")

        assert bonus == 4.0

    def test_bonus_stays_full_when_candidate_hit_by_healthy_signal(self):
        weights = {"sos": 1.0, "evr": 0.4, "trend_pullback": 1.0}
        hit_sets = {
            "sos": {"X002"},
            "evr": set(),
            "trend_pullback": set(),
            "spring": set(),
            "lps": set(),
            "compression": set(),
        }

        bonus = _track_alignment_bonus("X002", True, hit_sets, weights, "NEUTRAL")

        assert bonus == 10.0

    def test_bonus_uses_best_of_multiple_hit_signals(self):
        weights = {"sos": 1.0, "evr": 0.4, "trend_pullback": 1.0}
        hit_sets = {
            "sos": {"X003"},
            "evr": {"X003"},
            "trend_pullback": set(),
            "spring": set(),
            "lps": set(),
            "compression": set(),
        }

        bonus = _track_alignment_bonus("X003", True, hit_sets, weights, "NEUTRAL")

        assert bonus == 10.0

    def test_decayed_evr_candidate_scores_lower_than_healthy_sos_candidate(self):
        result = FunnelResult(
            layer1_symbols=["EVR001", "SOS001"],
            layer2_symbols=["EVR001", "SOS001"],
            layer3_symbols=["EVR001", "SOS001"],
            top_sectors=[],
            triggers={"evr": [("EVR001", 10.0)], "sos": [("SOS001", 10.0)]},
            stage_map={},
            markup_symbols=[],
            exit_signals={},
            channel_map={"EVR001": "趋势延续", "SOS001": "趋势延续"},
            leader_radar_symbols=[],
            leader_radar_rows=[],
        )

        _trend, _accum, scores = allocate_ai_candidates(
            result,
            [],
            "NEUTRAL",
            policy_override={
                "total_cap": 2,
                "trend_quota": 2,
                "accum_quota": 0,
                "max_trend_l3_fill": 0,
                "max_accum_l3_fill": 0,
            },
            signal_weight_map={"sos": 1.0, "evr": 0.4},
        )

        assert scores["SOS001"] > scores["EVR001"]


def _workflow_job_env(path: str, job_name: str) -> dict[str, str]:
    workflow = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return {**(workflow.get("env") or {}), **(workflow["jobs"][job_name].get("env") or {})}
