"""影子账本飞书卡：开仓行展示净收益，已平仓不进持仓列表。"""

from datetime import date

from core.shadow_ledger import ShadowBook, ShadowPosition, ShadowSession
from workflows.shadow_ledger_card import format_open_net_pnl, render_shadow_card


def _pos(
    code: str,
    name: str,
    *,
    shares: int,
    avg_cost: float,
    last_mark: float | None,
    sellable: int | None = None,
) -> ShadowPosition:
    return ShadowPosition(
        code=code,
        name=name,
        shares=shares,
        sellable_shares=shares if sellable is None else sellable,
        avg_cost=avg_cost,
        last_mark=last_mark,
    )


def _session(*positions: ShadowPosition) -> ShadowSession:
    return ShadowSession(
        book=ShadowBook(cash=50_000.0, positions={pos.code: pos for pos in positions}),
        fills=[],
        new_plans=[],
        nav={"cash": 50_000.0, "equity": 80_000.0, "market_value": 30_000.0, "pnl_total": -20_000.0},
    )


def test_open_position_line_shows_signed_net_pnl_and_pct() -> None:
    """净收益 = shares * (last_mark − avg_cost)；avg_cost 已含买费。"""
    title, content = render_shadow_card(
        _session(_pos("002292", "奥飞娱乐", shares=1000, avg_cost=10.0, last_mark=11.0)),
        date(2026, 9, 18),
    )
    assert title == "📒 影子账本 / paper 2026-09-18"
    assert "002292 奥飞娱乐  1000股  成本10.00  现价11.00  净收益 +1,000.00 (+10.0%)  可卖1000" in content
    assert "**现金** 50,000.00" in content
    assert "**累计** -20,000.00" in content


def test_losing_open_position_uses_minus_sign() -> None:
    _, content = render_shadow_card(
        _session(_pos("002584", "西陇科学", shares=500, avg_cost=8.0, last_mark=7.2, sellable=0)),
        date(2026, 9, 18),
    )
    assert "002584 西陇科学  500股  成本8.00  现价7.20  净收益 -400.00 (-10.0%)  可卖0" in content


def test_closed_or_zero_share_names_are_omitted_from_holdings() -> None:
    _, content = render_shadow_card(
        _session(
            _pos("002292", "奥飞娱乐", shares=800, avg_cost=5.0, last_mark=5.5),
            _pos("600000", "已抛浦发", shares=0, avg_cost=10.0, last_mark=12.0),
        ),
        date(2026, 9, 18),
    )
    holdings = content.split("**持仓**", 1)[1]
    assert "002292 奥飞娱乐" in holdings
    assert "600000" not in holdings
    assert "已抛浦发" not in holdings


def test_empty_book_shows_empty_holdings_not_closed_history() -> None:
    _, content = render_shadow_card(_session(), date(2026, 9, 18))
    assert "**持仓**\n空仓" in content
    assert "净收益" not in content.split("**持仓**", 1)[1]


def test_missing_mark_falls_back_to_cost_so_pnl_is_zero() -> None:
    _, content = render_shadow_card(
        _session(_pos("000001", "平安银行", shares=200, avg_cost=9.5, last_mark=None)),
        date(2026, 9, 18),
    )
    assert "现价9.50  净收益 +0.00 (+0.0%)" in content


def test_format_open_net_pnl_omits_pct_when_cost_basis_is_zero() -> None:
    assert format_open_net_pnl(100, 0.0, 10.0) == "净收益 +1,000.00"
    assert format_open_net_pnl(0, 8.0, 9.0) == "净收益 +0.00"
