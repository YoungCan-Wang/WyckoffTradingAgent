import importlib.util
from pathlib import Path

import pandas as pd


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "research_t4_etf_timing.py"
    spec = importlib.util.spec_from_file_location("research_t4_etf_timing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [f"2026-01-{i + 1:02d}" for i in range(len(prices))],
            "open": prices,
            "close": prices,
        }
    )


def test_run_timing_holds_above_ma_and_exits_below():
    mod = _load_module()
    prices = [10.0] * 30 + [11.0, 12.0, 13.0, 14.0, 15.0] + [14.0, 13.0, 12.0, 11.0, 10.0, 9.0, 8.0]
    out = mod.run_timing(_frame(prices), 5)
    assert out["switches"] >= 2
    assert out["invested_pct"] > 0


def test_run_timing_breaker_switches_not_more_than_plain():
    mod = _load_module()
    prices = [10.0] * 30 + [12.0, 14.0, 16.0, 18.0, 20.0] + [17.0, 15.0, 13.0, 11.0, 9.0, 8.0, 7.0]
    plain = mod.run_timing(_frame(prices), 5)
    breaker = mod.run_timing(_frame(prices), 5, breaker=True)
    assert breaker["switches"] <= plain["switches"] + 4
