from __future__ import annotations

import pytest

from core.trade_fill import Fill
from integrations import supabase_portfolio as sp


def _patch_fill_writers(monkeypatch, captured: dict, writer: str) -> None:
    def fake_write(portfolio_id, position, client=None, **_kwargs):
        captured["position"] = dict(position)
        captured["writer"] = writer
        return True, "ok"

    def fake_cash(portfolio_id, free_cash, client=None, **_kwargs):
        captured["cash"] = float(free_cash)
        return True, "ok"

    monkeypatch.setattr(sp, writer, fake_write)
    monkeypatch.setattr(sp, "upsert_position", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("upsert")))
    monkeypatch.setattr(sp, "update_free_cash", fake_cash)
    monkeypatch.setattr(
        sp,
        "refresh_portfolio_total_equity",
        lambda *_args, **_kwargs: sp.EquityRefreshResult(True, 1, "ok"),
    )


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
    monkeypatch.setattr(sp, "_fill_fx_to_cny", lambda _code: 0.92)
    _patch_fill_writers(monkeypatch, captured, "update_position")

    result = sp.record_fill("USER_LIVE:u1", fill, client=object())

    assert result.ok is True
    assert captured["writer"] == "update_position"
    assert captured["position"]["code"] == "00700.HK"
    assert captured["position"]["shares"] == 1100
    assert captured["position"]["name"] == "腾讯控股"
    # 港股买入含费用（印花税 0.1% 双边 + 佣金 + 杂费），故现金比毛额少扣一点。
    gross_cny = 100 * 300.0 * 0.92
    assert captured["cash"] < 100_000.0 - gross_cny
    assert captured["cash"] == pytest.approx(100_000.0 - gross_cny, rel=0.002)


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
    monkeypatch.setattr(sp, "_fill_fx_to_cny", lambda _code: 7.0)
    _patch_fill_writers(monkeypatch, captured, "update_position")

    result = sp.record_fill("USER_LIVE:u1", fill, client=object())

    assert result.ok is True
    assert captured["writer"] == "update_position"
    assert captured["position"]["code"] == "AAPL.US"
    assert captured["position"]["shares"] == 5
    # 美股卖出扣 SEC 规费与 FINRA TAF 后入账，金额很小但不为零。
    gross_cny = 5 * 200.0 * 7.0
    assert captured["cash"] < 1_000.0 + gross_cny
    assert captured["cash"] == pytest.approx(1_000.0 + gross_cny, rel=0.001)


def test_record_fill_rejects_foreign_when_fx_missing(monkeypatch):
    fill = Fill(code="AAPL.US", side="buy", shares=1, price=200.0, trade_date="20260815", name="")
    called = {"cash": False}

    def fake_cash(*_args, **_kwargs):
        called["cash"] = True
        return True, "ok"

    monkeypatch.setattr(sp, "_resolve_write_client", lambda client, _action: client or object())
    monkeypatch.setattr(
        sp,
        "load_portfolio_state",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("缺汇率时不得读仓")),
    )

    def missing_fx(_code: str) -> float:
        raise ValueError("缺少 USD->CNY 汇率，拒绝回填以免把外币金额写入人民币现金")

    monkeypatch.setattr(sp, "_fill_fx_to_cny", missing_fx)
    monkeypatch.setattr(sp, "update_free_cash", fake_cash)

    result = sp.record_fill("USER_LIVE:u1", fill, client=object())

    assert result.ok is False
    assert "汇率" in result.message
    assert called["cash"] is False
