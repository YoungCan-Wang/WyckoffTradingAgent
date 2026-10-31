from __future__ import annotations

from datetime import date

from integrations import fundamental_history as fh


def test_fetch_trims_fields_and_old_records(monkeypatch) -> None:
    class _Client:
        def __init__(self, api_key: str) -> None:
            pass

        def get_financial_metrics(self, symbols, *, latest):
            assert latest is False
            return {
                "600519.SH": [
                    {"announce_date": "2024-04-26", "period_end": "2024-03-31", "roe": 9.0, "eps_basic": 1.0},
                    {"announce_date": "2018-04-26", "period_end": "2018-03-31", "roe": 8.0},
                    {"announce_date": None, "period_end": "2024-06-30", "roe": 7.0},
                ],
                "000001.SZ": [],
            }

    monkeypatch.setattr("integrations.tickflow_client.TickFlowClient", _Client)
    out = fh.fetch_financial_history("k", ["600519", "000001"], since=date(2024, 6, 1))
    assert list(out) == ["600519"]
    assert out["600519"] == [
        {
            key: {"announce_date": "2024-04-26", "period_end": "2024-03-31", "roe": 9.0}.get(key)
            for key in fh.HISTORY_FIELDS
        }
    ]
