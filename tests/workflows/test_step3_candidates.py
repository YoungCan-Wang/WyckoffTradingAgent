from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pandas as pd

from workflows import step3_candidates as step3_candidates_mod
from workflows.step3_candidates import _base_candidate_fields
from workflows.step3_models import Step3MarketContext
from workflows.step3_runtime_config import Step3RuntimeConfig


def test_base_candidate_fields_preserves_candidate_attribution() -> None:
    row = _base_candidate_fields(
        0,
        {
            "code": "300308",
            "name": "中际旭创",
            "strategy_version": "candidate_lane_v1",
            "candidate_lane": "mainline",
            "entry_type": "主线平台再突破",
            "mainline_score": 0.86,
            "timing_score": 0.72,
        },
    )

    assert row["candidate_lane"] == "mainline"
    assert row["entry_type"] == "主线平台再突破"
    assert row["mainline_score"] == 0.86
    assert row["timing_score"] == 0.72


def test_build_step3_candidate_bundle_parallel_preserves_input_order(monkeypatch) -> None:
    completed: list[str] = []
    release_a = Event()

    def fake_load_history(code, _window, *, enforce_target_trade_date):
        if code == "A":
            release_a.wait(timeout=1)
        if code == "B":
            release_a.set()
        completed.append(code)
        if code == "B":
            return None, "bad history"
        return _history_frame(), None

    monkeypatch.setattr(step3_candidates_mod, "_load_step3_history", fake_load_history)

    bundle = step3_candidates_mod.build_step3_candidate_bundle(
        [
            {"code": "A", "name": "alpha", "track": "Trend"},
            {"code": "B", "name": "beta", "track": "Trend"},
            {"code": "C", "name": "gamma", "track": "Accum"},
        ],
        _market_context(),
        Step3RuntimeConfig(history_max_workers=3),
    )

    assert completed != ["A", "B", "C"]
    assert bundle.candidates_df["code"].tolist() == ["A", "C"]
    assert bundle.candidates_df["input_order"].tolist() == [0, 2]
    assert list(bundle.code_to_df) == ["A", "C"]
    assert bundle.failed == [("B", "bad history")]


def _market_context() -> Step3MarketContext:
    return Step3MarketContext(
        window=SimpleNamespace(start_trade_date="20250101", end_trade_date="20251231"),
        benchmark_context={},
        regime="NEUTRAL",
        sector_rotation_ctx={},
        sector_rotation_map={},
        sector_map={},
        market_cap_map={},
        financial_map={},
        benchmark_ret_10=1.0,
    )


def _history_frame() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=230, freq="B")
    close = pd.Series([10.0 + idx * 0.01 for idx in range(len(dates))])
    return pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "volume": [1_000_000.0] * len(dates),
            "amount": [100_000_000.0] * len(dates),
        }
    )
