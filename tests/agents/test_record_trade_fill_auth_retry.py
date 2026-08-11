from __future__ import annotations

from agents import portfolio_tools
from agents.portfolio_tools import _record_fill_with_safe_auth_retry
from core.trade_fill import Fill
from integrations.supabase_portfolio import PARTIAL_FILL_WRITE_MSG, FillWriteResult


class DummyToolContext:
    def __init__(self, state: dict[str, str] | None = None):
        self.state = state or {
            "user_id": "user-1",
            "access_token": "access",
            "refresh_token": "refresh",
        }


def test_partial_cash_write_failure_is_not_auth_retried(monkeypatch):
    calls: list[object] = []
    stale_client = object()

    def fake_record_fill(portfolio_id, fill, *, client):
        calls.append(client)
        if client is stale_client:
            return FillWriteResult(False, PARTIAL_FILL_WRITE_MSG, position_committed=True)
        raise AssertionError("auth retry must not re-apply a partially written fill")

    monkeypatch.setattr(portfolio_tools, "get_user_client", lambda _ctx: stale_client)
    monkeypatch.setattr(
        "integrations.supabase_portfolio.record_fill",
        fake_record_fill,
    )
    monkeypatch.setattr(
        portfolio_tools,
        "with_auth_retry",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("with_auth_retry should not run")),
    )

    result = _record_fill_with_safe_auth_retry(
        "user-1_LIVE",
        Fill(code="000001", side="buy", shares=1000, price=10.0, trade_date="20260730"),
        DummyToolContext(),
    )

    assert result == FillWriteResult(False, PARTIAL_FILL_WRITE_MSG, position_committed=True)
    assert calls == [stale_client]


def test_pre_write_auth_failure_still_retries_once(monkeypatch):
    calls: list[object] = []
    stale_client = object()
    fresh_client = object()

    def fake_record_fill(portfolio_id, fill, *, client):
        calls.append(client)
        if client is stale_client:
            return FillWriteResult(False, "{'message': 'JWT expired', 'code': 'PGRST303'}")
        return FillWriteResult(True, "ok", position_committed=True)

    monkeypatch.setattr(portfolio_tools, "get_user_client", lambda _ctx: stale_client)
    monkeypatch.setattr("integrations.supabase_portfolio.record_fill", fake_record_fill)

    def fake_with_auth_retry(_tool_context, fn, *args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["client"] = fresh_client
        return fn(*args, **kwargs)

    monkeypatch.setattr(portfolio_tools, "with_auth_retry", fake_with_auth_retry)

    result = _record_fill_with_safe_auth_retry(
        "user-1_LIVE",
        Fill(code="000001", side="buy", shares=1000, price=10.0, trade_date="20260730"),
        DummyToolContext(),
    )

    assert result == FillWriteResult(True, "ok", position_committed=True)
    assert calls == [stale_client, fresh_client]
