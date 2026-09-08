from __future__ import annotations

import pandas as pd
import pytest

from workflows.recommendation_event_eval import (
    RecommendationEventEvalRequest,
    _build_events,
    _build_summary,
    _decision_candidate,
    _ranking_strategy_decision,
    _write_markdown,
)


@pytest.mark.parametrize("net_return", [-10.0, 0.0, None])
def test_high_hit_ranking_cannot_promote_losing_or_missing_net_returns(net_return) -> None:
    events = []
    for day in range(20260501, 20260513):
        for code, score, shadow, hit, net in (("A", 99, 25, False, 1.0), ("B", 30, 88, True, net_return)):
            events.append(
                {
                    "recommend_date": day,
                    "code": code,
                    "funnel_score": score,
                    "candidate_shadow_score": shadow,
                    "label_ready": True,
                    "hit_target": hit,
                    "mfe_horizon_pct": 20 if hit else 5,
                    "mae_horizon_pct": -3,
                    "net_close_return_horizon_pct": net,
                }
            )

    decision = _build_summary(events, (1,))["ranking_decision"]
    candidate = decision["candidates"]["candidate_shadow_then_score"]

    assert candidate["hit_rate_delta_pct"] == 100.0
    assert candidate["sample_ok"] is True
    assert candidate["lift_ok"] is True
    assert candidate["net_return_ok"] is False
    assert candidate["status"] != "candidate"
    assert decision["recommended_strategy"] == "score_only"


@pytest.mark.parametrize(
    "override",
    [
        {"avg_net_close_return_delta_pct": 0.0},
        {"avg_net_close_return_delta_pct": -0.1},
        {"avg_net_close_return_delta_pct": None},
        {"avg_net_close_return_horizon_pct": float("nan")},
        {"avg_net_close_return_horizon_pct": float("inf")},
        {"net_return_rows": 11},
        {"baseline_net_return_rows": 11},
    ],
)
def test_candidate_requires_positive_net_lift_and_complete_finite_samples(override) -> None:
    decision = _decision_candidate("quality", "1", {**_lift_row(), **override})

    assert decision["status"] != "candidate"
    assert decision["net_return_ok"] is False


def test_valid_net_candidate_is_not_hidden_by_higher_hit_losing_top_k() -> None:
    top_rows = {
        "1": {**_lift_row(), "hit_rate_delta_pct": 100.0, "avg_net_close_return_horizon_pct": -10.0},
        "3": _lift_row(),
    }

    decision = _ranking_strategy_decision("quality", top_rows)

    assert decision["status"] == "candidate"
    assert decision["top_k"] == "3"
    assert decision["net_sample_ok"] is True


def test_event_builder_preserves_invalid_first_observed_bar() -> None:
    hist = _price_history()
    hist.loc[1, "high"] = float("nan")
    request = RecommendationEventEvalRequest(horizon_days=1)
    row = {"code": "000001", "recommend_date": 20260501, "created_at": "2026-05-01T12:00:00Z"}

    event = _build_events({"000001.SZ": [row]}, {"000001.SZ": hist}, request)[0]

    assert event["entry_date"] == 20260504
    assert event["label_status"] == "invalid_price_window"
    assert event["label_ready"] is False
    assert event["availability_verified"] is False


def test_event_builder_passes_normalized_hk_code_to_cost_and_execution_checks() -> None:
    hist = _price_history()
    hist.loc[1:, ["open", "high", "low", "close"]] *= 1.2
    request = RecommendationEventEvalRequest(market="hk", horizon_days=1)
    row = {"code": 700, "recommend_date": 20260501, "created_at": "2026-05-01T12:00:00Z"}

    event = _build_events({"00700.HK": [row]}, {"00700.HK": hist}, request)[0]

    assert event["code"] == "00700.HK"
    assert event["label_status"] == "ready"
    assert event["availability_verified"] is True
    assert event["round_trip_cost_pct"] > 0


def test_markdown_labels_event_basis_net_denominators_and_research_limits(tmp_path) -> None:
    events = [
        {"code": "A", "recommend_date": 20260501, "label_ready": False, "label_status": "missing_entry_open"},
        {
            "code": "B",
            "recommend_date": 20260501,
            "label_ready": True,
            "label_status": "ready",
            "close_return_horizon_pct": 1.2,
            "net_close_return_horizon_pct": 1.0,
        },
    ]
    result = {
        "metadata": {
            "market": "cn",
            "target_pct": 10,
            "horizon_days": 5,
            "records": 2,
            "codes": 2,
            "limitations": ["未模拟盘口和账户持仓"],
        },
        "summary": _build_summary(events, (1,)),
    }
    path = tmp_path / "summary.md"

    _write_markdown(path, result)
    markdown = path.read_text()

    assert "next_observed_open" in markdown
    assert "入场日记为0" in markdown and "H+1根日线" in markdown
    assert "不是首次入池累计涨幅" in markdown
    assert "A股排除入场当日" in markdown and "price_touch_target包含入场日" in markdown
    assert "| all | 2 | 1 | 1 | 1 | 1.2% | 1.0% | 100.0% |" in markdown
    assert '"missing_entry_open": 1' in markdown
    assert "净均值/胜率的分母是Net rows" in markdown
    assert "未模拟盘口和账户持仓" in markdown
    assert "样本内研究候选" in markdown and "仍需跨期组合验收" in markdown


def _lift_row() -> dict:
    return {
        "rows_ready": 12,
        "baseline_rows_ready": 12,
        "net_return_rows": 12,
        "baseline_net_return_rows": 12,
        "avg_net_close_return_horizon_pct": 2.0,
        "avg_net_close_return_delta_pct": 1.0,
        "hit_rate_delta_pct": 10.0,
        "avg_mfe_delta_pct": 2.0,
        "avg_mae_delta_pct": 0.0,
    }


def _price_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-05-01", "2026-05-04", "2026-05-05"],
            "open": [10.0, 10.0, 10.0],
            "high": [10.0, 10.0, 10.0],
            "low": [10.0, 10.0, 10.0],
            "close": [10.0, 10.0, 10.0],
            "volume": [100, 100, 100],
        }
    )
