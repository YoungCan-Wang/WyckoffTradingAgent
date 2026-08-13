from __future__ import annotations

from agents import portfolio_tools


def test_upsert_allows_hk_name_when_code_not_in_a_share_map(monkeypatch) -> None:
    monkeypatch.setattr(portfolio_tools, "code_to_name", lambda code: code)
    monkeypatch.setattr(portfolio_tools, "has_cloud", lambda _ctx: False)

    captured: dict = {}

    def fake_local(portfolio_id, code, name, shares, cost_price, buy_dt):
        captured.update(
            {
                "portfolio_id": portfolio_id,
                "code": code,
                "name": name,
                "shares": shares,
                "cost_price": cost_price,
                "buy_dt": buy_dt,
            }
        )
        return True

    monkeypatch.setattr("integrations.local_db.insert_local_position", fake_local)
    monkeypatch.setattr(portfolio_tools, "_portfolio_id", lambda _ctx: "USER_LIVE:local")
    monkeypatch.setattr(portfolio_tools, "_sync_remote_portfolio_to_local", lambda *_a, **_k: None)
    monkeypatch.setattr(
        portfolio_tools,
        "_local_update_summary",
        lambda portfolio_id, msg, cloud: {"message": msg, "portfolio_id": portfolio_id, "cloud": cloud},
    )

    result = portfolio_tools.update_portfolio(
        action="add",
        code="06881.HK",
        name="中国银河",
        shares=1000,
        cost_price=7.68,
        buy_dt="20260807",
        tool_context=None,
    )
    assert "error" not in result
    assert captured["code"] == "06881.HK"
    assert captured["name"] == "中国银河"
