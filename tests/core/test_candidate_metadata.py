from __future__ import annotations

import pytest

from core.candidate_metadata import (
    build_candidate_metadata_map,
    build_candidate_signal_metadata_map,
    candidate_lane_dedup_conflicts,
    candidate_metadata_for_signal,
    candidate_signal_triggers,
)
from core.candidate_tracks import (
    CANDIDATE_PRODUCER_TAGS,
    FORMAL_L4_LANES,
    WYCKOFF_STAGE_NAMES,
    best_candidate_entry_map,
)


def test_build_candidate_metadata_map_keeps_highest_scored_duplicate_entry() -> None:
    metadata = build_candidate_metadata_map(
        [
            {"code": "000001", "entry_type": "launchpad", "signal_key": "launchpad", "score": 80.0},
            {"code": "000001", "entry_type": "spring", "signal_key": "spring", "score": 100.0},
            {"code": "000001", "entry_type": "launchpad", "signal_key": "launchpad", "score": 70.0},
        ]
    )

    assert metadata["000001"]["entry_type"] == "spring"
    assert metadata["000001"]["signal_key"] == "spring"


def test_best_candidate_entry_map_sanitizes_output_score() -> None:
    entry_map = best_candidate_entry_map([{"code": "000001", "entry_type": "spring", "score": float("inf")}])

    assert entry_map["000001"]["score"] == 0.0


def test_build_candidate_metadata_map_ignores_invalid_duplicate_score() -> None:
    metadata = build_candidate_metadata_map(
        [
            {"code": "000001", "entry_type": "launchpad", "signal_key": "launchpad", "score": float("nan")},
            {"code": "000001", "entry_type": "spring", "signal_key": "spring", "score": 80.0},
        ]
    )

    assert metadata["000001"]["entry_type"] == "spring"
    assert metadata["000001"]["signal_key"] == "spring"


def test_candidate_signal_triggers_keeps_highest_duplicate_signal_score() -> None:
    triggers = candidate_signal_triggers(
        [
            {"code": "000001", "entry_type": "Early-Breakout", "score": 1.0},
            {"code": "000001", "entry_type": "early_breakout", "score": 9.0},
        ]
    )

    assert triggers == {"early_breakout": [("000001", 9.0)]}


def test_candidate_signal_triggers_treats_invalid_scores_as_zero() -> None:
    triggers = candidate_signal_triggers(
        [
            {"code": "000001", "entry_type": "Early-Breakout", "score": float("nan")},
            {"code": "000001", "entry_type": "early_breakout", "score": 9.0},
            {"code": "000002", "entry_type": "early_breakout", "score": float("inf")},
        ]
    )

    assert triggers == {"early_breakout": [("000001", 9.0), ("000002", 0.0)]}


def test_candidate_metadata_signal_key_prefers_structured_signal_over_display_text() -> None:
    metadata = build_candidate_metadata_map(
        [{"code": "300308", "entry_type": "主线回踩MA20", "signal_key": "mainline", "score": 86.0}]
    )

    assert metadata["300308"]["entry_type"] == "主线回踩MA20"
    assert metadata["300308"]["signal_key"] == "mainline"


def test_candidate_metadata_materializes_report_semantics() -> None:
    metadata = build_candidate_metadata_map(
        [{"code": "300308", "entry_type": "mainline", "signal_key": "mainline", "score": 86.0}],
        [
            {
                "code": "300308",
                "theme": "光模块",
                "status": "强主线分歧",
                "stock_role_score": 0.82,
                "mainline_score": 0.86,
            }
        ],
    )

    assert metadata["300308"]["candidate_theme"] == "光模块"
    assert metadata["300308"]["candidate_phase"] == "分歧机会"
    assert metadata["300308"]["candidate_role"] == "主线核心"


def test_signal_metadata_does_not_copy_trend_pullback_attribution_to_lps() -> None:
    metadata = build_candidate_signal_metadata_map(
        [{"code": "001872", "signal_key": "trend_pullback", "entry_type": "trend_pullback", "score": 68.0}]
    )

    assert candidate_metadata_for_signal(metadata, "001872", "trend_pullback")["signal_key"] == "trend_pullback"
    assert candidate_metadata_for_signal(metadata, "001872", "lps") == {}


def test_formal_signal_keeps_identity_and_inherits_mainline_context() -> None:
    metadata = build_candidate_signal_metadata_map(
        [{"code": "300308", "signal_key": "sos", "entry_type": "sos", "score": 91.0}],
        [
            {
                "code": "300308",
                "theme": "光模块",
                "status": "强主线分歧",
                "stock_role_score": 0.82,
                "mainline_score": 0.86,
            }
        ],
    )

    row = candidate_metadata_for_signal(metadata, "300308", "sos")
    assert row["signal_key"] == "sos"
    assert row["candidate_lane"] == "sos"
    assert row.get("candidate_status") is None
    assert row["candidate_theme"] == "光模块"
    assert row["candidate_phase"] == "分歧机会"
    assert row["candidate_role"] == "主线核心"


class TestCandidateStatusIsSemanticOnly:
    """candidate_status 只放语义状态，不放生产者标签，也不放 Wyckoff 阶段名。

    2026-09-01 在生产 signal_observations 上实测：7318 行里 6391 行（87%）存的是
    生产者标签（``Lane`` 3507、``alpha`` 2025、``formal_l4`` 799、``shadow_observe``
    60），另有 104 行存的是阶段名（``Accum_C`` 56、``Accum_B`` 45、``Markup`` 3）。
    交叉表显示这些值 100% 复述 ``candidate_lane``，这一列等于白占。

    两个后果：
    1. ``_tracking_status`` 里 ``if existing: return existing`` 把标签当成已有状态，
       那 6391 行永远拿不到真状态（``AI复核候选``/``跨日确认观察``/``市场拦截观察``）。
    2. ``_formal_candidate_entries`` 写的是 ``stage_map.get(code, "formal_l4")``,
       stage 已知时 ``formal_l4`` 这个标记本身被顶掉，漏斗效果检验按状态位建 L4
       集合就漏了 104 只正式候选，还把它们算进了对照池。
    """

    @staticmethod
    def _status(state: str, lane: str = "sos") -> str | None:
        item = {"code": "000001", "lane": lane, "entry_type": lane, "signal_key": lane, "state": state, "score": 80.0}
        return build_candidate_metadata_map([item])[item["code"]].get("candidate_status")

    @pytest.mark.parametrize("tag", sorted(CANDIDATE_PRODUCER_TAGS))
    def test_producer_tag_never_lands_in_status(self, tag: str) -> None:
        assert self._status(tag) is None

    @pytest.mark.parametrize("stage", sorted(WYCKOFF_STAGE_NAMES))
    def test_stage_name_never_lands_in_status(self, stage: str) -> None:
        """阶段名有自己的 stage/stage_tag 列，实测那 104 行两处都存了同一个值。"""
        assert self._status(stage) is None

    def test_lane_still_carries_the_channel(self) -> None:
        """过滤状态位不能把通道信息一起丢掉——通道由 candidate_lane 承载。"""
        item = {"code": "000001", "lane": "lps", "entry_type": "lps", "signal_key": "lps", "state": "Accum_C"}
        meta = build_candidate_metadata_map([item])["000001"]
        assert meta["candidate_lane"] == "lps"
        assert meta["candidate_lane"] in FORMAL_L4_LANES

    def test_real_semantic_status_on_state_survives(self) -> None:
        """影子/过热路径确实往 state 上写语义状态，这些必须留下。"""
        assert self._status("过热不追") == "过热不追"
        assert self._status("shadow") == "shadow"

    def test_mainline_status_comes_from_mainline_context(self) -> None:
        """主线路径的状态来自 mainline.status,不受 state 过滤影响。"""
        metadata = build_candidate_metadata_map(
            [{"code": "300308", "lane": "mainline", "entry_type": "主线回踩MA5", "state": "Mainline", "score": 80.0}],
            [{"code": "300308", "status": "主线买点候选", "theme": "光模块", "mainline_score": 0.86}],
        )
        assert metadata["300308"]["candidate_status"] == "主线买点候选"

    def test_formal_lane_does_not_inherit_mainline_observe_status(self) -> None:
        """非 mainline 车道不得继承「主线观察」，否则 Step4 确认闸门会静默否决。

        is_confirmed_step4_candidate 对 candidate_status 做「观察」子串否决，且排在
        signal_status/selection_source 的正向确认之前。#366 滤掉 formal_l4 后若再回退
        到 mainline 默认态「主线观察」，跨日已确认的 SOS 会被当成未确认丢掉。
        """
        from core.mainline_engine import MAINLINE_OBSERVE_STATUS
        from workflows.step4_pipeline import is_confirmed_step4_candidate

        formal = {
            "code": "002292",
            "lane": "sos",
            "entry_type": "sos",
            "signal_key": "sos",
            "state": "formal_l4",
            "score": 108.0,
        }
        mainline = {
            "code": "002292",
            "status": MAINLINE_OBSERVE_STATUS,
            "theme": "测试主题",
            "mainline_score": 0.8,
        }
        meta = build_candidate_metadata_map([formal], [mainline])["002292"]
        assert meta["candidate_lane"] == "sos"
        assert meta.get("candidate_status") is None
        assert meta["candidate_theme"] == "测试主题"

        confirmed = {
            "code": "002292",
            "signal_status": "confirmed",
            "status": "confirmed",
            "selection_source": "signal_confirmed",
            "tag": "SOS(跨日确认)",
            "candidate_lane": meta["candidate_lane"],
            "candidate_status": meta.get("candidate_status"),
        }
        assert is_confirmed_step4_candidate(confirmed) is True

    def test_formal_l4_lanes_cover_every_formal_trigger(self) -> None:
        """FORMAL_L4_LANES 要与 _formal_candidate_entries 的 base_map 键一致。

        漏一条通道就等于把那条通道的票判进对照池,超额会被自家兄弟稀释。
        """
        from core.wyckoff_engine import _formal_candidate_entries

        triggers = {key: [("000001", 1.0)] for key in FORMAL_L4_LANES}
        entries = _formal_candidate_entries(triggers, {}, {})
        assert {entry["entry_type"] for entry in entries} == set(FORMAL_L4_LANES)
        assert {entry["state"] for entry in entries} == {"formal_l4"}


def test_lane_dedup_conflict_counts_only_real_disagreements() -> None:
    """判别性用例：同一只票双命中,但两种去重规则挑出不同通道时才该计数。

    launchpad 优先级 0(设计上该赢),trend_breakout 优先级 6;实测 score 却反过来
    (2026-09-02 候选池中位 88.27 vs 98.00)。所以按 score 去重拿到 trend_breakout,
    按优先级去重拿到 launchpad —— 这是要数的那一类。
    """
    stats = candidate_lane_dedup_conflicts(
        [
            {"code": "000001", "entry_type": "launchpad", "score": 88.0},
            {"code": "000001", "entry_type": "trend_breakout", "score": 98.0},
        ]
    )

    assert stats["multi_lane_codes"] == 1
    assert stats["disagreed_codes"] == 1
    assert stats["details"][0]["score_pick"] == "trend_breakout"
    assert stats["details"][0]["priority_pick"] == "launchpad"


def test_lane_dedup_conflict_ignores_agreeing_and_single_lane_codes() -> None:
    """两种规则一致、或压根没双命中的,都不该进计数,否则这个观测量没有判别性。"""
    stats = candidate_lane_dedup_conflicts(
        [
            # 双命中但一致:launchpad 优先级更高且分也更高
            {"code": "000002", "entry_type": "launchpad", "score": 99.0},
            {"code": "000002", "entry_type": "trend_breakout", "score": 70.0},
            # 单车道:同通道多条不算双命中
            {"code": "000003", "entry_type": "lps", "score": 30.0},
            {"code": "000003", "entry_type": "lps", "score": 40.0},
        ]
    )

    assert stats["codes"] == 2
    assert stats["multi_lane_codes"] == 1
    assert stats["disagreed_codes"] == 0
    assert stats["details"] == []
