from __future__ import annotations

import pandas as pd


def test_get_market_history_returns_tickflow_digest(monkeypatch):
    import agents.market_tools as mt

    calls = []

    class DummyTickFlowClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        def get_klines(self, symbol: str, **kwargs):
            calls.append({"api_key": self.api_key, "symbol": symbol, **kwargs})
            return pd.DataFrame(
                [
                    {"date": "2026-01-02", "open": 100, "high": 103, "low": 99, "close": 100, "volume": 1000},
                    {"date": "2026-01-03", "open": 102, "high": 106, "low": 101, "close": 105, "volume": 1300},
                    {"date": "2026-01-04", "open": 105, "high": 105, "low": 100, "close": 101, "volume": 1600},
                ]
            )

    monkeypatch.setattr(mt, "get_credential", lambda *_args, **_kwargs: "tf-key")
    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", DummyTickFlowClient)

    result = mt.get_market_history(days=3, index="上证")

    assert result["ok"] is True
    assert result["source"] == "tickflow"
    assert result["index"]["symbol"] == "000001.SH"
    assert result["returned_days"] == 3
    assert result["summary"]["period_return_pct"] == 1.0
    assert result["rows"][-1]["date"] == "2026-01-04"
    assert calls == [{"api_key": "tf-key", "symbol": "000001.SH", "period": "1d", "count": 20, "adjust": "none"}]
