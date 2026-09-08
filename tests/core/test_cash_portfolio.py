from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.cash_portfolio import CashPortfolioConfig, calc_trade_cost, expand_portfolio_styles, simulate_cash_portfolio

FEE_FREE = {"commission_rate": 0.0, "min_commission": 0.0, "stamp_duty_rate": 0.0, "transfer_fee_rate": 0.0}


def test_commission_floors_at_min_commission() -> None:
    cfg = CashPortfolioConfig(commission_rate=0.0002, min_commission=5, stamp_duty_rate=0.0, transfer_fee_rate=0.0)

    # 万 2 费率下，佣金要到 25000 元成交额才追平 5 元下限。
    assert calc_trade_cost(5_000, cfg, side="buy") == 5.0
    assert calc_trade_cost(10_000, cfg, side="buy") == 5.0
    assert calc_trade_cost(25_000, cfg, side="buy") == 5.0
    assert calc_trade_cost(100_000, cfg, side="buy") == 20.0


def test_sell_side_adds_stamp_duty_and_both_sides_pay_transfer_fee() -> None:
    cfg = CashPortfolioConfig(
        commission_rate=0.0002, min_commission=0.0, stamp_duty_rate=0.0005, transfer_fee_rate=0.00001
    )

    assert calc_trade_cost(100_000, cfg, side="buy") == pytest.approx(20.0 + 1.0)
    assert calc_trade_cost(100_000, cfg, side="sell") == pytest.approx(20.0 + 1.0 + 50.0)


def test_cash_portfolio_limits_positions_and_lot_size() -> None:
    rows = []
    for idx in range(5):
        rows.append(
            {
                "code": f"00000{idx}",
                "name": f"S{idx}",
                "signal_date": "2026-01-02",
                "entry_date": "2026-01-05",
                "exit_date": "2026-01-10",
                "entry_close": 10.0,
                "exit_close": 11.0,
            }
        )

    closed, nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(
            initial_cash=100_000,
            max_positions=4,
            commission_rate=0.0002,
            min_commission=5,
            lot_size=100,
        ),
    )

    assert len(closed) == 4
    assert set(closed["shares"]) == {2400}
    assert summary["cash_portfolio_skipped_full"] == 1
    assert summary["cash_portfolio_win_rate_pct"] == 100.0
    assert summary["cash_portfolio_final_cash"] > 109_000
    assert summary["cash_portfolio_max_drawdown_pct"] <= 0
    assert not nav.empty


def test_cash_portfolio_preserves_trade_exit_reason() -> None:
    rows = [
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-10",
            "entry_close": 10.0,
            "exit_close": 11.8,
            "exit_reason": "take_profit",
            "regime": "NEUTRAL",
        }
    ]

    closed, _nav, _summary = simulate_cash_portfolio(pd.DataFrame(rows), CashPortfolioConfig(initial_cash=100_000))

    assert closed.iloc[0]["exit_reason"] == "take_profit"
    assert closed.iloc[0]["regime"] == "NEUTRAL"


def test_cash_portfolio_applies_research_entry_weight_multiplier() -> None:
    rows = [
        {
            "code": "000001",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-10",
            "entry_close": 10.0,
            "exit_close": 10.0,
            "entry_weight_multiplier": 0.5,
        }
    ]
    config = CashPortfolioConfig(
        initial_cash=100_000,
        max_positions=4,
        **FEE_FREE,
    )

    closed, _nav, _summary = simulate_cash_portfolio(pd.DataFrame(rows), config)

    assert closed.iloc[0]["shares"] == 1200
    assert closed.iloc[0]["entry_weight_multiplier"] == 0.5


def test_cash_portfolio_accepts_empty_trade_frame() -> None:
    closed, nav, summary = simulate_cash_portfolio(pd.DataFrame(), CashPortfolioConfig(initial_cash=100_000))

    assert closed.empty
    assert nav.empty
    assert summary["cash_portfolio_final_cash"] == 100_000
    assert summary["cash_portfolio_max_drawdown_pct"] == 0.0
    assert summary["cash_portfolio_trades"] == 0


@pytest.mark.parametrize("timing_mode", ["legacy_exit_first", "open_before_exit"])
def test_cash_portfolio_applies_execution_friction_once_to_cash_positions_and_nav(timing_mode) -> None:
    trades = pd.DataFrame(
        [
            {
                "code": "000001",
                "entry_date": "2026-01-05",
                "exit_date": "2026-01-06",
                "entry_close": 10.0,
                "exit_close": 10.0,
                "ret_pct": -99.0,
            }
        ]
    )
    config = CashPortfolioConfig(
        initial_cash=100_000,
        max_positions=1,
        buy_friction_pct=1.0,
        sell_friction_pct=1.0,
        cash_timing_mode=timing_mode,
        **FEE_FREE,
    )

    closed, nav, summary = simulate_cash_portfolio(trades, config, entry_mark_price_fn=lambda _code, _day: 10.0)

    trade = closed.iloc[0]
    assert trade["shares"] == 9_900
    assert trade["entry_market_price"] == 10.0
    assert trade["entry_price"] == 10.1
    assert trade["exit_market_price"] == 10.0
    assert trade["exit_price"] == 9.9
    assert trade["buy_friction_cost"] == pytest.approx(990.0)
    assert trade["sell_friction_cost"] == pytest.approx(990.0)
    assert trade["ret_pct"] == pytest.approx(-1.9801980198)
    assert nav.iloc[0]["equity"] == pytest.approx(99_010.0)
    assert summary["cash_portfolio_final_cash"] == pytest.approx(98_020.0)
    assert summary["cash_portfolio_friction_total"] == pytest.approx(1_980.0)
    assert summary["cash_portfolio_buy_friction_pct"] == 1.0
    assert summary["cash_portfolio_sell_friction_pct"] == 1.0


def test_cash_portfolio_drawdown_uses_mark_price() -> None:
    rows = [
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-20",
            "entry_close": 10.0,
            "exit_close": 10.0,
        },
        {
            "code": "000002",
            "name": "S2",
            "signal_date": "2026-01-03",
            "entry_date": "2026-01-06",
            "exit_date": "2026-01-20",
            "entry_close": 10.0,
            "exit_close": 10.0,
        },
    ]

    _closed, _nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, max_positions=2),
        mark_price_fn=lambda code, day: 8.0 if code == "000001" and day == date(2026, 1, 6) else 10.0,
    )

    assert summary["cash_portfolio_max_drawdown_pct"] < -9.0


def test_portfolio_style_probe_add_allows_same_stock_addon() -> None:
    rows = [
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-20",
            "entry_close": 10.0,
            "exit_close": 11.0,
            "score": 1.0,
        },
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-06",
            "entry_date": "2026-01-07",
            "exit_date": "2026-01-22",
            "entry_close": 10.5,
            "exit_close": 11.5,
            "score": 1.2,
        },
    ]

    closed, _nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, portfolio_style="probe_add"),
    )

    assert list(closed["entry_kind"]) == ["probe", "add"]
    assert summary["cash_portfolio_probe_entries"] == 1
    assert summary["cash_portfolio_add_entries"] == 1


def test_portfolio_style_addon_uses_current_mark_for_target_weight() -> None:
    rows = [
        {
            "code": "000001",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-20",
            "entry_close": 10.0,
            "exit_close": 20.0,
        },
        {
            "code": "000001",
            "entry_date": "2026-01-07",
            "exit_date": "2026-01-22",
            "entry_close": 20.0,
            "exit_close": 20.0,
        },
    ]
    config = CashPortfolioConfig(
        initial_cash=100_000,
        portfolio_style="probe_add",
        **FEE_FREE,
    )

    closed, _nav, _summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        config,
        mark_price_fn=lambda code, day: 20.0 if code == "000001" and day == date(2026, 1, 7) else None,
    )

    assert closed.loc[closed["entry_kind"] == "add", "shares"].iloc[0] == 200


def test_portfolio_style_confirmation_uses_pending_pool_confirmation() -> None:
    rows = [
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-20",
            "entry_close": 10.0,
            "exit_close": 11.0,
            "score": 1.0,
            "signal_confirmed": False,
        },
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-06",
            "entry_date": "2026-01-07",
            "exit_date": "2026-01-22",
            "entry_close": 10.5,
            "exit_close": 11.5,
            "score": 1.2,
            "signal_confirmed": True,
        },
    ]

    closed, _nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, portfolio_style="confirmation_only"),
    )

    assert list(closed["entry_kind"]) == ["confirmed"]
    assert summary["cash_portfolio_unconfirmed"] == 1
    assert summary["cash_portfolio_confirmed_entries"] == 1


def test_portfolio_style_confirmation_does_not_treat_repeat_as_confirmation() -> None:
    rows = [
        {
            "code": "000001",
            "entry_date": date(2026, 1, day),
            "exit_date": date(2026, 1, day + 5),
            "entry_close": 10.0,
            "exit_close": 11.0,
            "signal_confirmed": False,
        }
        for day in (5, 7)
    ]

    closed, _nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, portfolio_style="confirmation_only"),
    )

    assert closed.empty
    assert summary["cash_portfolio_unconfirmed"] == 2


def test_portfolio_style_concentrated_swap_replaces_weak_holding() -> None:
    rows = [
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-02-01",
            "entry_close": 10.0,
            "exit_close": 9.0,
            "score": 1.0,
        },
        {
            "code": "000002",
            "name": "S2",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-02-01",
            "entry_close": 10.0,
            "exit_close": 9.0,
            "score": 1.1,
        },
        {
            "code": "000003",
            "name": "S3",
            "signal_date": "2026-01-06",
            "entry_date": "2026-01-07",
            "exit_date": "2026-02-03",
            "entry_close": 10.0,
            "exit_close": 12.0,
            "score": 2.0,
        },
    ]

    closed, _nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, portfolio_style="concentrated_swap"),
        mark_price_fn=lambda code, day: 10.2 if code == "000001" and day == date(2026, 1, 7) else None,
    )

    assert "style_swap" in set(closed["exit_reason"])
    assert "000003" in set(closed["code"])
    assert summary["cash_portfolio_style_swaps"] == 1


def test_portfolio_style_concentrated_swap_sanitizes_nonfinite_scores() -> None:
    rows = [
        {
            "code": "000001",
            "name": "S1",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-02-01",
            "entry_close": 10.0,
            "exit_close": 9.0,
            "score": float("inf"),
        },
        {
            "code": "000002",
            "name": "S2",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-02-01",
            "entry_close": 10.0,
            "exit_close": 9.0,
            "score": float("inf"),
        },
        {
            "code": "000003",
            "name": "S3",
            "signal_date": "2026-01-06",
            "entry_date": "2026-01-07",
            "exit_date": "2026-02-03",
            "entry_close": 10.0,
            "exit_close": 12.0,
            "score": 2.0,
        },
    ]

    closed, _nav, summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, portfolio_style="concentrated_swap"),
        mark_price_fn=lambda code, day: 10.2 if code == "000001" and day == date(2026, 1, 7) else None,
    )

    assert "style_swap" in set(closed["exit_reason"])
    assert summary["cash_portfolio_style_swaps"] == 1
    assert set(closed["score"]) == {0.0, 2.0}


def test_cash_portfolio_releases_position_on_its_own_exit_date() -> None:
    """A position must be marked closed on its own exit_date, not deferred to the next
    signal day. Otherwise the NAV curve reports a stale (still-open) position and cash
    balance for every day in between, which would corrupt any point-in-time cash/slot
    availability check that runs during that gap."""
    rows = [
        {
            "code": "000001",
            "name": "A",
            "signal_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-08",
            "entry_close": 10.0,
            "exit_close": 9.0,
        },
        {
            "code": "000002",
            "name": "B",
            "signal_date": "2026-01-19",
            "entry_date": "2026-01-20",
            "exit_date": "2026-01-25",
            "entry_close": 10.0,
            "exit_close": 11.0,
        },
    ]

    _closed, nav, _summary = simulate_cash_portfolio(
        pd.DataFrame(rows),
        CashPortfolioConfig(initial_cash=100_000, max_positions=1),
    )

    exit_row = nav[nav["date"] == date(2026, 1, 8)].iloc[0]
    assert exit_row["positions"] == 0
    assert exit_row["cash"] == exit_row["equity"]


def test_expand_portfolio_styles_preset() -> None:
    assert expand_portfolio_styles("all_core") == [
        "slot_equal_4",
        "probe_add",
        "confirmation_only",
        "trend_pyramid",
        "concentrated_swap",
    ]


def test_open_first_does_not_use_same_day_exit_cash_or_slot_and_releases_next_day() -> None:
    trades = pd.DataFrame([_cash_row("A", 5, 6), _cash_row("B", 6, 8), _cash_row("C", 7, 8)])
    config = CashPortfolioConfig(max_positions=1, cash_timing_mode="open_before_exit", **FEE_FREE)

    closed, nav, summary = simulate_cash_portfolio(trades, config, entry_mark_price_fn=lambda _code, _day: 10.0)
    legacy, legacy_nav, legacy_summary = simulate_cash_portfolio(
        trades, CashPortfolioConfig(max_positions=1, **FEE_FREE)
    )

    assert list(closed["code"]) == ["A", "C"]
    assert list(legacy["code"]) == ["A", "B"]
    assert summary["cash_portfolio_skipped_full"] == 1
    assert summary["cash_portfolio_timing_mode"] == "open_before_exit"
    assert legacy_summary["cash_portfolio_timing_mode"] == "legacy_exit_first"
    assert nav.set_index("date").loc[date(2026, 1, 6), "cash"] == 100_000
    assert nav.set_index("date").loc[date(2026, 1, 6), "positions"] == 0
    assert legacy_nav.set_index("date").loc[date(2026, 1, 6), "positions"] == 1


@pytest.mark.parametrize("style", ["slot_equal_4", "confirmation_only"])
def test_open_first_sizing_uses_open_marks_not_current_close_but_nav_uses_close(style) -> None:
    trades = pd.DataFrame([_cash_row("A", 5, 8), _cash_row("B", 6, 8)])
    config = CashPortfolioConfig(
        max_positions=2, equal_weight=0.5, portfolio_style=style, cash_timing_mode="open_before_exit", **FEE_FREE
    )
    runs = [
        simulate_cash_portfolio(
            trades,
            config,
            mark_price_fn=lambda code, _day, mark=closing_mark: mark if code == "A" else 10.0,
            entry_mark_price_fn=lambda _code, _day: 10.0,
        )
        for closing_mark in (5.0, 20.0)
    ]

    assert list(runs[0][0]["shares"]) == list(runs[1][0]["shares"]) == [5000, 5000]
    assert runs[0][1].set_index("date").loc[date(2026, 1, 6), "equity"] == 75_000
    assert runs[1][1].set_index("date").loc[date(2026, 1, 6), "equity"] == 150_000


@pytest.mark.parametrize("missing_mark", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_open_first_missing_active_open_mark_skips_entry_without_close_fallback(missing_mark) -> None:
    trades = pd.DataFrame([_cash_row("A", 5, 8), _cash_row("B", 6, 8)])
    config = CashPortfolioConfig(max_positions=2, cash_timing_mode="open_before_exit", **FEE_FREE)

    closed, _nav, summary = simulate_cash_portfolio(
        trades,
        config,
        mark_price_fn=lambda _code, _day: 20.0,
        entry_mark_price_fn=lambda _code, _day: missing_mark,
    )

    assert list(closed["code"]) == ["A"]
    assert summary["cash_portfolio_skipped_entry_mark"] == 1
    assert summary["cash_portfolio_final_cash"] == 100_000


def test_open_first_rejects_t0_ledger_and_does_not_reenter_same_code_before_exit() -> None:
    trades = pd.DataFrame([_cash_row("T0", 5, 5), _cash_row("A", 5, 6), _cash_row("A", 6, 8), _cash_row("A", 7, 8)])
    config = CashPortfolioConfig(max_positions=2, cash_timing_mode="open_before_exit", **FEE_FREE)

    closed, _nav, summary = simulate_cash_portfolio(trades, config, entry_mark_price_fn=lambda _code, _day: 10.0)

    assert list(closed["entry_date"]) == [date(2026, 1, 5), date(2026, 1, 7)]
    assert all(closed["exit_date"] > closed["entry_date"])
    assert summary["cash_portfolio_skipped_t1"] == 1
    assert summary["cash_portfolio_skipped_duplicate"] == 1


def test_open_first_requires_explicit_open_mark_and_known_timing_mode() -> None:
    with pytest.raises(ValueError, match="entry_mark_price_fn"):
        simulate_cash_portfolio(pd.DataFrame(), CashPortfolioConfig(cash_timing_mode="open_before_exit"))
    with pytest.raises(ValueError, match="未知 cash_timing_mode"):
        simulate_cash_portfolio(pd.DataFrame(), CashPortfolioConfig(cash_timing_mode="unknown"))


@pytest.mark.parametrize("style", ["probe_add", "trend_pyramid", "concentrated_swap"])
def test_open_first_rejects_portfolio_styles_with_unvalidated_intraday_semantics(style) -> None:
    config = CashPortfolioConfig(portfolio_style=style, cash_timing_mode="open_before_exit")

    with pytest.raises(ValueError, match="其他组合样式需单独验收"):
        simulate_cash_portfolio(pd.DataFrame(), config, entry_mark_price_fn=lambda _code, _day: 10.0)


def test_legacy_default_keeps_current_mark_sizing_and_ignores_new_open_callback() -> None:
    trades = pd.DataFrame([_cash_row("A", 5, 8), _cash_row("B", 6, 8)])
    config = CashPortfolioConfig(max_positions=2, **FEE_FREE)

    def close_mark(code, _day):
        return 5.0 if code == "A" else 10.0

    default = simulate_cash_portfolio(trades, config, mark_price_fn=close_mark)
    explicit = simulate_cash_portfolio(
        trades, config, mark_price_fn=close_mark, entry_mark_price_fn=lambda _code, _day: 100.0
    )

    pd.testing.assert_frame_equal(default[0], explicit[0])
    pd.testing.assert_frame_equal(default[1], explicit[1])
    assert default[2] == explicit[2]
    assert list(default[0]["shares"]) == [5000, 3700]


def _cash_row(code: str, entry_day: int, exit_day: int) -> dict:
    return {
        "code": code,
        "entry_date": date(2026, 1, entry_day),
        "exit_date": date(2026, 1, exit_day),
        "entry_close": 10.0,
        "exit_close": 10.0,
        "signal_confirmed": True,
    }
