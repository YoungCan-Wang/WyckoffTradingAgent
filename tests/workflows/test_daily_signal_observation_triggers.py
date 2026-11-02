from __future__ import annotations

from workflows.daily_signal_observations import _merge_observation_trigger_maps, _primary_observation_triggers


def test_observation_triggers_keep_calibrated_candidate_score() -> None:
    details = {
        "triggers": {"spring": [("000001", 0.82)]},
        "candidate_entries": [{"code": "000001", "signal_key": "spring", "score": 78.5}],
        "metrics": {"external_seed_l4_triggers": {"spring": [("000002", 0.5)]}},
    }

    assert _primary_observation_triggers(details)["spring"] == [("000001", 78.5)]
    assert _merge_observation_trigger_maps(details)["spring"] == [("000001", 78.5), ("000002", 0.5)]
