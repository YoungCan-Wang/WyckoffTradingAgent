from __future__ import annotations

import pandas as pd

from workflows.step3_entry_quality import annotate_entry_quality, entry_quality_policy_tag
from workflows.step3_runtime_config import Step3RuntimeConfig
from workflows.step3_selection import select_step3_candidates
from workflows.step3_upstream_selection import select_upstream_priority_candidates


def test_entry_quality_marks_clean_base_and_risks() -> None:
    df = pd.DataFrame(
        [
            {
                "code": "GOOD",
                "priority_score": 88.0,
                "rs_10": 9.0,
                "min_vol_ratio_5d": 0.62,
                "bias_200": 12.0,
                "avg_amount_20_yi": 4.5,
            },
            {
                "code": "HOT",
                "priority_score": 86.0,
                "rs_10": -2.0,
                "min_vol_ratio_5d": 1.8,
                "bias_200": 55.0,
                "avg_amount_20_yi": 0.6,
            },
        ]
    )

    annotated = annotate_entry_quality(df).set_index("code")

    assert annotated.loc["GOOD", "entry_quality_grade"] == "S"
    assert "入场质量S" in entry_quality_policy_tag(annotated.loc["GOOD"])
    assert annotated.loc["HOT", "entry_quality_grade"] == "D"
    assert annotated.loc["HOT", "entry_risk_flags"] == "弱于指数、缩量不足、追高延展、成交额偏低"


def test_upstream_priority_uses_entry_quality_within_priority_bucket() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "HOT", "track": "Trend", "input_order": 0, "priority_score": 89.4, "rs_10": -1.0},
            {
                "code": "BASE",
                "track": "Trend",
                "input_order": 1,
                "priority_score": 89.1,
                "rs_10": 8.0,
                "min_vol_ratio_5d": 0.65,
                "bias_200": 10.0,
                "avg_amount_20_yi": 3.0,
            },
            {
                "code": "HIGHER_BUCKET",
                "track": "Trend",
                "input_order": 2,
                "priority_score": 91.0,
                "rs_10": -5.0,
            },
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["HIGHER_BUCKET", "BASE"]
    assert "entry_quality_sort_bucket" not in selected.columns


def test_upstream_priority_keeps_materially_higher_score_before_entry_quality() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "HOT", "track": "Trend", "input_order": 0, "priority_score": 89.0, "rs_10": -1.0},
            {
                "code": "BASE",
                "track": "Trend",
                "input_order": 1,
                "priority_score": 86.0,
                "rs_10": 8.0,
                "min_vol_ratio_5d": 0.65,
                "bias_200": 10.0,
                "avg_amount_20_yi": 3.0,
            },
            {
                "code": "HIGHER_BUCKET",
                "track": "Trend",
                "input_order": 2,
                "priority_score": 91.0,
                "rs_10": -5.0,
            },
        ]
    )

    selected = select_upstream_priority_candidates(candidates, Step3RuntimeConfig(), context_cap=2)

    assert selected["code"].tolist() == ["HIGHER_BUCKET", "HOT"]


def test_upstream_priority_can_disable_entry_quality_sorting() -> None:
    candidates = pd.DataFrame(
        [
            {"code": "HOT", "track": "Trend", "input_order": 0, "priority_score": 89.4, "rs_10": -1.0},
            {
                "code": "BASE",
                "track": "Trend",
                "input_order": 1,
                "priority_score": 89.1,
                "rs_10": 8.0,
                "min_vol_ratio_5d": 0.65,
                "bias_200": 10.0,
                "avg_amount_20_yi": 3.0,
            },
        ]
    )

    selected = select_upstream_priority_candidates(
        candidates,
        Step3RuntimeConfig(entry_quality_tie_bucket=0),
        context_cap=1,
    )

    assert selected["code"].tolist() == ["HOT"]


def test_select_step3_candidates_attaches_entry_quality_fields() -> None:
    selected = select_step3_candidates(
        pd.DataFrame(
            [
                {
                    "code": "BASE",
                    "track": "Trend",
                    "priority_score": 80.0,
                    "rs_10": 5.0,
                    "min_vol_ratio_5d": 0.7,
                    "bias_200": 12.0,
                    "avg_amount_20_yi": 2.0,
                }
            ]
        ),
        "NEUTRAL",
        Step3RuntimeConfig(enable_compression=False),
    )

    assert selected.loc[0, "entry_quality_score"] > 0
    assert selected.loc[0, "entry_quality_tag"].startswith("入场质量")
