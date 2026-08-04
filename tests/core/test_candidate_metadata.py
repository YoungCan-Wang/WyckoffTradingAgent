from __future__ import annotations

from core.candidate_metadata import (
    build_candidate_metadata_map,
    build_candidate_signal_metadata_map,
    candidate_metadata_for_signal,
    candidate_signal_triggers,
)
from core.candidate_tracks import best_candidate_entry_map


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
    assert row["candidate_theme"] == "光模块"
    assert row["candidate_phase"] == "分歧机会"
    assert row["candidate_role"] == "主线核心"
