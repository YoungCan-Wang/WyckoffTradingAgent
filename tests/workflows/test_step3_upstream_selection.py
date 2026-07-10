from __future__ import annotations

import pandas as pd

from workflows.step3_batch_report import _runtime_config_for_items
from workflows.step3_runtime_config import Step3RuntimeConfig
from workflows.step3_selection import select_step3_candidates
from workflows.step3_upstream_selection import select_upstream_priority_candidates


def test_explicit_report_input_forces_upstream_priority() -> None:
    cfg = Step3RuntimeConfig(respect_upstream_priority=False)

    got = _runtime_config_for_items(cfg, [{"code": "000001", "selection_source": "explicit_report_input"}])

    assert got.respect_upstream_priority is True


def test_non_explicit_report_input_keeps_runtime_priority_setting() -> None:
    cfg = Step3RuntimeConfig(respect_upstream_priority=False)

    got = _runtime_config_for_items(cfg, [{"code": "000001", "selection_source": "signal_confirmed"}])

    assert got.respect_upstream_priority is False


def test_upstream_priority_selection_uses_priority_score_under_cap() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "T_LOW", "track": "Trend", "input_order": 0, "priority_score": 10.0},
            {"code": "T_HIGH", "track": "Trend", "input_order": 1, "priority_score": 90.0},
            {"code": "A_HIGH", "track": "Accum", "input_order": 2, "priority_score": 80.0},
            {"code": "A_LOW", "track": "Accum", "input_order": 3, "priority_score": 20.0},
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["T_HIGH", "A_HIGH"]


def test_upstream_priority_selection_keeps_priority_order_after_cap() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "T_MID", "track": "Trend", "input_order": 0, "priority_score": 50.0},
            {"code": "T_LOW", "track": "Trend", "input_order": 1, "priority_score": 10.0},
            {"code": "T_HIGH", "track": "Trend", "input_order": 2, "priority_score": 90.0},
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["T_HIGH", "T_MID"]


def test_upstream_priority_selection_preserves_input_order_without_scores() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "T_FIRST", "track": "Trend", "input_order": 0, "priority_score": pd.NA},
            {"code": "T_SECOND", "track": "Trend", "input_order": 1, "priority_score": pd.NA},
            {"code": "A_FIRST", "track": "Accum", "input_order": 2, "priority_score": pd.NA},
            {"code": "A_SECOND", "track": "Accum", "input_order": 3, "priority_score": pd.NA},
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["T_FIRST", "A_FIRST"]


def test_upstream_priority_selection_ignores_nonfinite_scores() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "T_INF", "track": "Trend", "input_order": 0, "priority_score": float("inf")},
            {"code": "T_GOOD", "track": "Trend", "input_order": 1, "priority_score": 90.0},
            {"code": "A_GOOD", "track": "Accum", "input_order": 2, "priority_score": 80.0},
            {"code": "A_NAN", "track": "Accum", "input_order": 3, "priority_score": float("nan")},
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["T_GOOD", "A_GOOD"]


def test_upstream_priority_selection_dedupes_cross_track_duplicate_before_cap() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "DUP", "track": "Trend", "input_order": 0, "priority_score": 90.0},
            {"code": "DUP", "track": "Accum", "input_order": 1, "priority_score": 80.0},
            {"code": "A_GOOD", "track": "Accum", "input_order": 2, "priority_score": 70.0},
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["DUP", "A_GOOD"]
    assert selected.loc[selected["code"] == "DUP", "track"].iloc[0] == "Trend"


def test_upstream_priority_selection_prefers_core_over_fill_duplicate() -> None:
    candidates = pd.DataFrame(
        [
            {
                "code": "DUP",
                "track": "Trend",
                "input_order": 0,
                "priority_score": 99.0,
                "selection_is_fill": True,
            },
            {
                "code": "DUP",
                "track": "Trend",
                "input_order": 1,
                "priority_score": 10.0,
                "selection_is_fill": False,
            },
            {
                "code": "KEEP",
                "track": "Accum",
                "input_order": 2,
                "priority_score": 20.0,
                "selection_is_fill": False,
            },
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=0)
    duplicate = selected[selected["code"] == "DUP"].iloc[0]

    assert selected["code"].tolist() == ["DUP", "KEEP"]
    assert bool(duplicate["selection_is_fill"]) is False
    assert duplicate["priority_score"] == 10.0


def test_step3_wyckoff_score_uses_finite_priority_then_funnel() -> None:
    candidates = pd.DataFrame(
        [
            {
                "code": "P_INF",
                "track": "Trend",
                "input_order": 0,
                "priority_score": float("inf"),
                "funnel_score": 8.0,
            },
            {
                "code": "F_INF",
                "track": "Trend",
                "input_order": 1,
                "priority_score": float("nan"),
                "funnel_score": float("inf"),
            },
        ]
    )

    selected = select_step3_candidates(
        candidates,
        "NEUTRAL",
        Step3RuntimeConfig(enable_compression=False, respect_upstream_priority=False),
    )

    by_code = selected.set_index("code")["wyckoff_score"]
    assert by_code["P_INF"] == 8.0
    assert pd.isna(by_code["F_INF"])
