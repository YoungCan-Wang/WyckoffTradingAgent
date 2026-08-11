from __future__ import annotations

from types import SimpleNamespace

from integrations import portfolio_market_value, supabase_portfolio


class _UpdateQuery:
    def __init__(self) -> None:
        self.payload: dict | None = None

    def update(self, payload: dict):
        self.payload = payload
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=[self.payload])


class _UpdateClient:
    def __init__(self) -> None:
        self.query = _UpdateQuery()

    def table(self, _name: str):
        return self.query


def test_refresh_portfolio_total_equity_persists_latest_multi_market_value(monkeypatch) -> None:
    state = {
        "free_cash": 25_000,
        "positions": [{"code": "600519", "shares": 10}, {"code": "06881.HK", "shares": 1000}],
    }
    client = _UpdateClient()
    monkeypatch.setattr(supabase_portfolio, "load_portfolio_state", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(supabase_portfolio, "_portfolio_tickflow_key", lambda *_args: "key")
    monkeypatch.setattr(
        portfolio_market_value,
        "load_portfolio_marks",
        lambda *_args: ({"600519": 1_500, "06881.HK": 7.63}, {"CNY": 1, "HKD": 0.86}),
    )

    result = supabase_portfolio.refresh_portfolio_total_equity("USER_LIVE:u1", client=client)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.total_equity == 46_561.8
    assert client.query.payload is not None
    assert client.query.payload["total_equity"] == 46_561.8
    assert client.query.payload["updated_at"]


def test_refresh_does_not_overwrite_total_when_a_quote_is_missing(monkeypatch) -> None:
    state = {"free_cash": 25_000, "positions": [{"code": "600519", "shares": 10}]}
    client = _UpdateClient()
    monkeypatch.setattr(supabase_portfolio, "load_portfolio_state", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(supabase_portfolio, "_portfolio_tickflow_key", lambda *_args: "key")
    monkeypatch.setattr(portfolio_market_value, "load_portfolio_marks", lambda *_args: ({}, {"CNY": 1}))

    result = supabase_portfolio.refresh_portfolio_total_equity("USER_LIVE:u1", client=client)  # type: ignore[arg-type]

    assert result.ok is False
    assert client.query.payload is None
