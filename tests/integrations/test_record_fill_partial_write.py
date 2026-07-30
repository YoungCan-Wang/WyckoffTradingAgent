from __future__ import annotations

from core.trade_fill import Fill
from integrations import supabase_portfolio as sp


def test_cash_write_failure_does_not_echo_auth_keywords(monkeypatch):
    fill = Fill(code="000001", side="buy", shares=1000, price=10.0, trade_date="20260730", name="测试")
    state = {"free_cash": 50_000.0, "positions": []}

    monkeypatch.setattr(sp, "_resolve_write_client", lambda client, _action: client or object())
    monkeypatch.setattr(sp, "load_portfolio_state", lambda _pid, client=None: state)
    monkeypatch.setattr(sp, "upsert_position", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        sp,
        "update_free_cash",
        lambda *_args, **_kwargs: (False, "{'message': 'JWT expired', 'code': 'PGRST303'}"),
    )

    result = sp.record_fill("user-1_LIVE", fill, client=object())

    assert result == sp.FillWriteResult(False, sp.PARTIAL_FILL_WRITE_MSG, position_committed=True)
    lowered = result.message.lower()
    assert "jwt" not in lowered
    assert "expired" not in lowered
    assert "token" not in lowered
