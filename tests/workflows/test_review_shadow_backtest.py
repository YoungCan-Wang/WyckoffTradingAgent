from datetime import date

import pandas as pd
import pytest

from core.funnel_taxonomy import REVIEW_STAGE_THEME_MISS, REVIEW_STAGE_TRIGGER_MISS
from workflows.review_shadow_backtest import evaluate_shadow_traces, summarize_shadow_trades


def _history(*, signal_pct: float = 2.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"]).date,
            "open": [10.0, 10.0, 10.6, 10.8],
            "high": [10.2, 10.7, 11.0, 11.2],
            "low": [9.8, 9.9, 10.4, 10.7],
            "close": [10.0, 10.5, 10.8, 11.0],
            "pct_chg": [signal_pct, 5.0, 2.86, 1.85],
        }
    )


def test_shadow_backtest_uses_trace_day_then_future_only_for_outcome() -> None:
    traces = [
        {
            "trade_date": date(2026, 5, 12).isoformat(),
            "symbols": {
                "000001": {
                    "name": "平安银行",
                    "stage": REVIEW_STAGE_TRIGGER_MISS,
                    "l3_eligible": True,
                    "l2_channel": "主升通道",
                }
            },
        }
    ]

    trades = evaluate_shadow_traces(traces, {"000001": _history()})
    report = summarize_shadow_trades(trades, traces, {"000001": _history()})

    assert len(trades) == 1
    assert trades[0].entry_date == "2026-05-13"
    assert trades[0].entry_open == 10.0
    assert trades[0].ret_t1_pct == pytest.approx(5.0)
    assert trades[0].review_hit is False
    assert report["uses_future_for_selection"] is False


def test_shadow_backtest_does_not_infer_lane_from_future_move() -> None:
    traces = [
        {
            "trade_date": "2026-05-12",
            "symbols": {"000001": {"stage": REVIEW_STAGE_THEME_MISS, "l2_eligible": False}},
        }
    ]

    assert evaluate_shadow_traces(traces, {"000001": _history()}) == []


def test_shadow_backtest_reports_review_recall_from_next_trade_day() -> None:
    history = _history()
    history.loc[1, "close"] = 10.8
    history.loc[1, "pct_chg"] = 8.0
    traces = [
        {
            "trade_date": "2026-05-12",
            "symbols": {
                "000001": {
                    "name": "平安银行",
                    "stage": REVIEW_STAGE_TRIGGER_MISS,
                    "l3_eligible": True,
                }
            },
        }
    ]

    trades = evaluate_shadow_traces(traces, {"000001": history})
    report = summarize_shadow_trades(trades, traces, {"000001": history})

    assert trades[0].review_hit is True
    assert report["review_recall"]["review_hits"] == 1
    assert report["review_recall"]["shadow_hits"] == 1
    assert report["review_shadow_recall_rate"] == 1.0


def test_shadow_backtest_requires_exact_next_market_trade_date() -> None:
    traces = [
        {
            "trade_date": "2026-05-12",
            "symbols": {"000001": {"stage": REVIEW_STAGE_TRIGGER_MISS, "l3_eligible": True}},
        }
    ]
    suspended = _history().iloc[[0, 2, 3]].reset_index(drop=True)
    market_calendar = _history()

    assert evaluate_shadow_traces(traces, {"000001": suspended, "000002": market_calendar}) == []


def _trigger_miss_trace(scores: dict[str, float | None]) -> list[dict]:
    return [
        {
            "trade_date": "2026-05-12",
            "symbols": {
                code: {
                    "name": f"票{code}",
                    "stage": REVIEW_STAGE_TRIGGER_MISS,
                    "l3_eligible": True,
                    "l2_channel": "主升通道",
                    **({} if score is None else {"layer3_quality_score": score}),
                }
                for code, score in scores.items()
            },
        }
    ]


def test_score_band_reports_unranked_when_every_score_ties() -> None:
    """分值全同要显式说「排序键无区分度」,不能悄悄把全体塞进一个桶。

    这就是 v1 的具体形态:pre_breakout 恒 78.0,分档表看着有三行,其实低中高
    是同一批票。让它自报 ranked=False,才不会有人拿这张表当单调性证据。
    """
    scores = {"000001": 0.42, "000002": 0.42, "000003": 0.42}
    traces = _trigger_miss_trace(scores)
    history = {code: _history() for code in scores}

    report = summarize_shadow_trades(evaluate_shadow_traces(traces, history), traces, history)
    band = report["by_score_band"]["pre_breakout"]

    assert band["ranked"] is False
    assert "分值全同" in band["reason"]


def test_score_band_splits_tertiles_once_scores_differ() -> None:
    """watch_score 有区分度时才给出低/中/高三档,切点要落在样本内。"""
    scores = {f"00000{i}": 0.1 * i for i in range(1, 7)}
    traces = _trigger_miss_trace(scores)
    history = {code: _history() for code in scores}

    report = summarize_shadow_trades(evaluate_shadow_traces(traces, history), traces, history)
    band = report["by_score_band"]["pre_breakout"]

    assert band["ranked"] is True
    lo, hi = band["cut_points"]
    assert lo < hi
    assert sum(band["bands"][name]["count"] for name in ("low", "mid", "high")) == len(scores)


def test_score_band_marks_rotation_setup_unrankable() -> None:
    """题材车道没有连续键,分档表只能标注不可排序,不能造一个假结论。"""
    traces = [
        {
            "trade_date": "2026-05-12",
            "symbols": {
                code: {"name": f"票{code}", "stage": REVIEW_STAGE_THEME_MISS, "l2_eligible": True}
                for code in ("000001", "000002", "000003")
            },
        }
    ]
    history = {code: _history() for code in ("000001", "000002", "000003")}

    report = summarize_shadow_trades(evaluate_shadow_traces(traces, history), traces, history)
    band = report["by_score_band"]["rotation_setup"]

    assert band["ranked"] is False
    # 不能说成「样本不足」:那会让人以为攒够数据就能分档,而这一层压根没有键。
    assert "无连续排序键" in band["reason"]
    assert "样本不足" not in band["reason"]
