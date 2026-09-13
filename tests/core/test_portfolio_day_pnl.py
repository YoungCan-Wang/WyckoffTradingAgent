from __future__ import annotations

from core.daily_nav_schema import DAY_PNL_KEYS, build_ddl
from core.portfolio_day_pnl import BASIS_PREV_CLOSE, BASIS_TODAY_FILL, calculate_book_day_pnl
from core.trade_fill import BUY, Fill, Holding, apply_fill


def test_day_pnl_uses_prev_close_and_sums_book() -> None:
    book = calculate_book_day_pnl(
        101.0,
        [
            {"code": "300628", "name": "亿联网络", "shares": 400, "cost": 42.231, "buy_dt": "2026-09-04"},
            {"code": "00285.HK", "name": "比亚迪电子", "shares": 500, "cost": 26.8, "buy_dt": "2026-09-01"},
        ],
        {"300628": 43.0, "00285.HK": 27.0},
        {"300628": 42.0, "00285.HK": 26.5},
        {"CNY": 1.0, "HKD": 0.9},
        "2026-09-11",
    )
    assert book.complete is True
    assert book.positions[0].day_pnl == 400.0
    assert book.positions[0].day_pnl_pct == 2.381
    assert book.positions[1].day_pnl == 225.0
    assert book.day_pnl == 625.0
    # 日初市值 400*42 + 500*26.5*0.9 = 28725，加现金 101
    assert book.day_pnl_pct == round(625.0 / 28_826.0 * 100.0, 4)


def test_same_day_open_with_prev_close_still_uses_prev_close() -> None:
    """有昨收时不看 buy_dt：T+1 加仓也会把 buy_dt 刷成今天，不能当整仓今开。"""
    book = calculate_book_day_pnl(
        0.0,
        [{"code": "600415", "name": "小商品城", "shares": 300, "cost": 13.0, "buy_dt": "2026-09-11"}],
        {"600415": 13.5},
        {"600415": 12.0},
        {"CNY": 1.0},
        "2026-09-11",
    )
    assert book.complete is True
    assert book.positions[0].basis_kind == BASIS_PREV_CLOSE
    assert book.positions[0].basis == 12.0
    assert book.positions[0].day_pnl == 450.0


def test_same_day_open_without_prev_close_falls_back_to_fill() -> None:
    book = calculate_book_day_pnl(
        0.0,
        [{"code": "600415", "name": "小商品城", "shares": 300, "cost": 13.0, "buy_dt": "2026-09-11"}],
        {"600415": 13.5},
        {},
        {"CNY": 1.0},
        "2026-09-11",
    )
    assert book.complete is True
    assert book.positions[0].basis_kind == BASIS_TODAY_FILL
    assert book.positions[0].basis == 13.0
    assert book.positions[0].day_pnl == 150.0


def test_addon_refreshed_buy_dt_does_not_inflate_day_pnl() -> None:
    """隔夜仓当日加仓后 buy_dt=今天、成本被摊薄；当日盈亏仍须 vs 昨收，不能 vs 均价。"""
    held = Holding(code="600519", name="茅台", shares=1000, cost_price=10.0, buy_dt="2026-01-01")
    filled = apply_fill(
        held,
        cash=1_000_000.0,
        fill=Fill(code="600519", side=BUY, shares=100, price=1505.0, trade_date="2026-09-12", name="茅台"),
    )
    assert filled.holding is not None
    assert filled.holding.buy_dt == "2026-09-12"
    pos = {
        "code": filled.holding.code,
        "name": filled.holding.name,
        "shares": filled.holding.shares,
        "cost": filled.holding.cost_price,
        "buy_dt": filled.holding.buy_dt,
    }
    book = calculate_book_day_pnl(
        filled.cash,
        [pos],
        {"600519": 1505.0},
        {"600519": 1500.0},
        {"CNY": 1.0},
        "2026-09-12",
    )
    assert book.complete is True
    assert book.positions[0].basis_kind == BASIS_PREV_CLOSE
    assert book.day_pnl == 5500.0


def test_incomplete_basis_is_not_a_book_total() -> None:
    book = calculate_book_day_pnl(
        0.0,
        [{"code": "300773", "shares": 500, "cost": 17.18, "buy_dt": "2026-09-04"}],
        {"300773": 17.5},
        {},
        {"CNY": 1.0},
        "2026-09-11",
    )
    assert book.complete is False
    assert book.missing == ("300773",)


def test_daily_nav_ddl_covers_upsert_keys() -> None:
    ddl = build_ddl()
    assert DAY_PNL_KEYS == {"day_pnl", "day_pnl_pct", "position_day_pnl"}
    for key in DAY_PNL_KEYS:
        assert f"add column if not exists {key}" in ddl
