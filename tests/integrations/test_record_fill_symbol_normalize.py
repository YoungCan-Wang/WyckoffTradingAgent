from __future__ import annotations

from core.trade_fill import Fill
from integrations import supabase_portfolio as sp


def test_record_fill_buy_matches_existing_hk_despite_case_and_padding(monkeypatch):
    """CLI 常见输入 700.HK / 00700.hk 必须命中已存的 00700.HK，否则会按零股重写覆盖。"""
    fill = Fill(code="700.HK", side="buy", shares=100, price=300.0, trade_date="20260808", name="")
    state = {
        "free_cash": 100_000.0,
        "positions": [
            {
                "code": "00700.HK",
                "name": "腾讯控股",
                "shares": 1000,
                "cost": 280.0,
                "buy_dt": "20260101",
            }
        ],
    }
    captured: dict = {}

    monkeypatch.setattr(sp, "_resolve_write_client", lambda client, _action: client or object())
    monkeypatch.setattr(sp, "load_portfolio_state", lambda _pid, client=None: state)

    def fake_upsert(portfolio_id, position, client=None, **_kwargs):
        captured["position"] = dict(position)
        return True, "ok"

    monkeypatch.setattr(sp, "upsert_position", fake_upsert)
    monkeypatch.setattr(sp, "update_free_cash", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        sp,
        "refresh_portfolio_total_equity",
        lambda *_args, **_kwargs: sp.EquityRefreshResult(True, 1, "ok"),
    )

    result = sp.record_fill("USER_LIVE:u1", fill, client=object())

    assert result.ok is True
    assert captured["position"]["code"] == "00700.HK"
    assert captured["position"]["shares"] == 1100
    assert captured["position"]["name"] == "腾讯控股"


def test_record_fill_rejects_invalid_code_before_write(monkeypatch):
    called = {"load": False}

    def fake_load(*_args, **_kwargs):
        called["load"] = True
        return {"free_cash": 1.0, "positions": []}

    monkeypatch.setattr(sp, "load_portfolio_state", fake_load)
    fill = Fill(code="6881", side="buy", shares=100, price=10.0, trade_date="20260808")
    result = sp.record_fill("USER_LIVE:u1", fill, client=object())
    assert result.ok is False
    assert "无效" in result.message
    assert called["load"] is False


def test_record_fill_sell_matches_us_bare_ticker(monkeypatch):
    fill = Fill(code="aapl", side="sell", shares=5, price=200.0, trade_date="20260808", name="")
    state = {
        "free_cash": 1_000.0,
        "positions": [{"code": "AAPL.US", "name": "Apple", "shares": 10, "cost": 180.0, "buy_dt": "20260101"}],
    }
    captured: dict = {}

    monkeypatch.setattr(sp, "_resolve_write_client", lambda client, _action: client or object())
    monkeypatch.setattr(sp, "load_portfolio_state", lambda _pid, client=None: state)

    def fake_upsert(portfolio_id, position, client=None, **_kwargs):
        captured["position"] = dict(position)
        return True, "ok"

    monkeypatch.setattr(sp, "upsert_position", fake_upsert)
    monkeypatch.setattr(sp, "update_free_cash", lambda *_args, **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        sp,
        "refresh_portfolio_total_equity",
        lambda *_args, **_kwargs: sp.EquityRefreshResult(True, 1, "ok"),
    )

    result = sp.record_fill("USER_LIVE:u1", fill, client=object())

    assert result.ok is True
    assert captured["position"]["code"] == "AAPL.US"
    assert captured["position"]["shares"] == 5
