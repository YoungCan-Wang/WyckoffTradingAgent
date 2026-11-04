"""Offline tests for the personal trade-review skill script (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "trade-review" / "scripts" / "trade_review.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("trade_review", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


TR = _load()


def _bars(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["d", "open", "high", "low", "close"])
    df["d"] = pd.to_datetime(df["d"])
    return TR.prepare_bars(df)


def _fills(rows: list[tuple], code: str = "600001.SH", name: str = "测试") -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["date", "side", "price", "qty"])
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = df["price"] * df["qty"]
    df["fee"] = 5.0
    df["code"], df["name"] = code, name
    return df


def _trip(rows, code="600001.SH"):
    trips, open_pos = TR.build_round_trips(_fills(rows, code=code))
    assert not open_pos
    return trips[0]


FLAT = [
    ("2026-01-05", 10.0, 10.2, 9.9, 10.0),
    ("2026-01-06", 10.0, 10.1, 9.5, 9.6),
    ("2026-01-07", 9.5, 9.6, 8.9, 9.0),
    ("2026-01-08", 9.1, 9.3, 8.8, 9.2),
    ("2026-01-09", 9.3, 9.6, 9.2, 9.5),
]


def test_round_trips_split_on_flat_and_report_open_positions(tmp_path):
    csv = tmp_path / "fills.csv"
    _fills(
        [
            ("2026-01-05", "buy", 10.0, 100),
            ("2026-01-06", "sell", 11.0, 100),
            ("2026-01-07", "buy", 10.0, 200),
            ("2026-01-08", "sell", 10.5, 100),
        ]
    ).to_csv(csv, index=False)
    trips, open_pos = TR.build_round_trips(TR.load_fills(csv))
    assert len(trips) == 1 and trips[0].end == pd.Timestamp("2026-01-06")
    assert open_pos == ["测试(600001.SH) 剩余 100 股"]
    assert TR.trip_pnl(trips[0].fills) == pytest.approx(1100 - 1000 - 10)


def test_load_fills_rejects_unknown_side(tmp_path):
    csv = tmp_path / "bad.csv"
    pd.DataFrame(
        [{"name": "x", "date": "2026-01-05", "side": "short", "price": 1, "qty": 1, "amount": 1, "fee": 0}]
    ).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="buy/sell"):
        TR.load_fills(csv)


def test_baseline_replay_reproduces_actual_pnl():
    trip = _trip(
        [("2026-01-05", "buy", 10.0, 1000), ("2026-01-06", "buy", 9.6, 500), ("2026-01-09", "sell", 9.5, 1500)]
    )
    bars = _bars(FLAT)
    assert TR.replay_trip(trip, bars, None) == pytest.approx(TR.trip_pnl(trip.fills))


def test_intraday_stop_respects_t_plus_one_and_fills_at_stop():
    trip = _trip([("2026-01-05", "buy", 10.0, 1000), ("2026-01-09", "sell", 9.5, 1000)])
    pnl = TR.replay_trip(trip, _bars(FLAT), {"stop": ("intra", 0.05)})
    avg_cost = (10000 + TR._buy_fee(10000)) / 1000  # stop is measured from fee-inclusive average cost
    stop_amount = avg_cost * 0.95 * 1000  # day 2 low 9.5 touches the stop, filled at the stop price
    assert pnl == pytest.approx(-10000 - TR._buy_fee(10000) + stop_amount - TR._sell_fee(stop_amount))


def test_one_price_limit_down_day_cannot_be_sold():
    bars = _bars(
        [
            ("2026-01-05", 10.0, 10.0, 9.8, 10.0),
            ("2026-01-06", 9.0, 9.0, 9.0, 9.0),  # one-price limit-down: unsellable
            ("2026-01-07", 8.5, 8.8, 8.2, 8.6),
            ("2026-01-08", 8.7, 8.9, 8.6, 8.8),
        ]
    )
    trip = _trip([("2026-01-05", "buy", 10.0, 1000), ("2026-01-08", "sell", 8.8, 1000)])
    pnl = TR.replay_trip(trip, bars, {"stop": ("intra", 0.05)})
    assert pnl == pytest.approx(-10000 - TR._buy_fee(10000) + 8500 - TR._sell_fee(8500))


def test_no_add_rule_skips_averaging_down_and_scales_the_exit():
    trip = _trip(
        [("2026-01-05", "buy", 10.0, 1000), ("2026-01-07", "buy", 9.0, 1000), ("2026-01-09", "sell", 9.5, 2000)]
    )
    pnl = TR.replay_trip(trip, _bars(FLAT), {"noadd": True})
    assert pnl == pytest.approx(-10000 - TR._buy_fee(10000) + 9500 - TR._sell_fee(9500))
    assert TR.averaged_down(trip)


def test_prior_limit_ups_use_board_specific_limits():
    days = pd.bdate_range("2025-12-01", periods=25)
    closes = [10.0] * 22 + [11.0, 12.1]  # two +10% days
    rows = [(d.strftime("%Y-%m-%d"), c, c, c, c) for d, c in zip(days[:24], closes, strict=True)]
    rows.append((days[24].strftime("%Y-%m-%d"), 12.1, 12.2, 12.0, 12.1))
    bars = _bars(rows)
    entry = days[24].strftime("%Y-%m-%d")
    main_board = _trip([(entry, "buy", 12.1, 100), (entry, "sell", 12.1, 100)], code="600001.SH")
    growth_board = _trip([(entry, "buy", 12.1, 100), (entry, "sell", 12.1, 100)], code="300001.SZ")
    assert TR.prior_features(main_board, bars)["prior_limit_ups"] == 2
    assert TR.prior_features(growth_board, bars)["prior_limit_ups"] == 0


def test_solve_lots_recovers_unique_fill():
    entry = pd.Series({"low": 9.8, "high": 10.3})
    exit_ = pd.Series({"low": 10.9, "high": 11.4})
    qty, buy_px, sell_px = TR.solve_lots(
        buy_amount=30150.0, pnl=2833.0, total_fee=17.0, entry_bar=entry, exit_bar=exit_
    )
    assert (qty, buy_px, sell_px) == (3000, 10.05, 11.0)


def test_rule_table_reports_leave_two_out_and_halves():
    bars = {"600001.SH": _bars(FLAT)}
    trips = [
        _trip([("2026-01-05", "buy", 10.0, 1000), ("2026-01-09", "sell", 9.5, 1000)]),
        _trip([("2026-01-06", "buy", 9.6, 1000), ("2026-01-09", "sell", 9.5, 1000)]),
    ]
    table = TR.rule_table(trips, bars, {"stop5": {"stop": ("intra", 0.05)}})
    row = table.iloc[0]
    assert row["变好笔数"] + row["变差笔数"] <= 2
    assert row["去掉最大2笔后"] == 0
    assert abs(row["前半样本"] + row["后半样本"] - row["相对实际"]) <= 1


def test_index_shadow_replays_cash_flows():
    closes = pd.Series([100.0, 110.0], index=pd.to_datetime(["2026-01-05", "2026-01-09"]))
    trip = _trip([("2026-01-05", "buy", 10.0, 1000), ("2026-01-09", "sell", 9.5, 1000)])
    assert TR.index_shadow_pnl(trip, closes) == pytest.approx(1000.0)


def test_output_inside_repo_is_refused():
    with pytest.raises(SystemExit):
        TR._guard_out_dir(TR.REPO_ROOT / "tmp" / "review", allow_repo=False)
    TR._guard_out_dir(Path("/nonexistent-outside-repo/review"), allow_repo=False)
