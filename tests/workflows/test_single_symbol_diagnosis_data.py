from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from workflows import single_symbol_diagnosis_data as data


@dataclass(frozen=True)
class Spec:
    market: str
    symbol: str
    label: str


def test_prepare_symbol_history_trims_to_requested_window():
    raw = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=8, freq="D").strftime("%Y-%m-%d"),
            "open": range(8),
            "high": range(8),
            "low": range(8),
            "close": range(8),
        }
    )

    result = data.prepare_symbol_history(raw, date(2026, 1, 5), date(2026, 1, 7), trading_days=2)

    assert result["date"].tolist() == ["2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06", "2026-01-07"]
    assert result["date_obj"].iloc[-1] == date(2026, 1, 7)


def test_load_symbol_context_returns_light_context_for_non_cn(monkeypatch):
    monkeypatch.setattr(data, "load_symbol_name_map", lambda markets: {"AAPL.US": "Apple"})

    context = data.load_symbol_context(
        Spec("us", "AAPL.US", "美股"), pd.DataFrame(), date(2026, 1, 1), date(2026, 1, 2)
    )

    assert context.name_map == {"AAPL.US": "Apple"}
    assert context.market_cap_map == {}
    assert context.sector_map == {}
    assert context.bench_df is None


def test_load_rps_universe_histories_normalizes_tickflow_batches(monkeypatch):
    raw = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3, freq="D").strftime("%Y-%m-%d"),
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
        }
    )

    class Client:
        def __init__(self, api_key: str):
            self.api_key = api_key

        def get_klines_batch(self, symbols, **_kwargs):
            return {symbol: raw for symbol in symbols}

    monkeypatch.setattr(data, "TickFlowClient", Client)
    monkeypatch.setattr(data, "get_stocks_by_board", lambda _board: [{"code": "000001"}, {"code": "000002"}])

    result = data.load_rps_universe_histories(Spec("cn", "000001", "A股"), date(2026, 1, 1), date(2026, 1, 3), 2)

    assert sorted(result) == ["000002"]
    assert len(result["000002"]) == 3
