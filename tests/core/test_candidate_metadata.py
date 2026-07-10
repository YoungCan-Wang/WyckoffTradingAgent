from __future__ import annotations

from core.candidate_metadata import build_candidate_metadata_map, candidate_signal_triggers
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
