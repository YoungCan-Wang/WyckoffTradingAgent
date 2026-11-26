"""影子账本已清仓回合：配对买卖、区间高低两端都含、仍持有不成回合。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.shadow_round_trips import (
    closed_round_trips,
    interval_high_low,
    needs_round_trip_payload,
    round_trip_payload,
    stored_round_trips,
)


def _event(day: str, code: str, action: str, price: float, qty: int, *, status: str = "filled", key: str = "") -> dict:
    return {
        "event_key": key or f"{day}:{action}:{code}:{qty}",
        "as_of": day,
        "code": code,
        "name": f"N{code}",
        "event_type": action,
        "price": price,
        "qty": qty,
        "payload": {"status": status},
    }


def _bars(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": [day for day, _h, _l in rows],
            "最高": [high for _d, high, _l in rows],
            "最低": [low for _d, _h, low in rows],
        }
    )


def test_pairs_buy_and_full_sell_into_one_round_trip_using_fill_prices() -> None:
    trips = closed_round_trips(
        [
            _event("2026-09-11", "301076", "sell", 20.65, 1000, key="sell-key"),
            _event("2026-09-01", "301076", "buy", 23.16, 1000),
        ]
    )

    assert len(trips) == 1
    trip = trips[0]
    assert (trip.code, trip.qty) == ("301076", 1000)
    assert (trip.buy_date, trip.buy_price) == (date(2026, 9, 1), 23.16)
    assert (trip.sell_date, trip.sell_price) == (date(2026, 9, 11), 20.65)
    assert trip.sell_event_key == "sell-key"


def test_still_open_position_is_not_a_round_trip() -> None:
    events = [
        _event("2026-10-26", "600406", "buy", 23.48, 900),
        _event("2026-11-03", "300872", "buy", 15.02, 1300),
        _event("2026-11-10", "300872", "sell", 14.80, 1300),
        _event("2026-11-20", "601012", "buy", 11.30, 2000),
        _event("2026-11-24", "601012", "sell", 12.00, 1000),
    ]

    assert [trip.code for trip in closed_round_trips(events)] == ["300872"]


def test_scaled_entries_and_exits_use_share_weighted_fill_prices() -> None:
    trips = closed_round_trips(
        [
            _event("2026-09-01", "000001", "buy", 10.0, 1000),
            _event("2026-09-03", "000001", "buy", 12.0, 1000),
            _event("2026-09-05", "000001", "sell", 13.0, 500),
            _event("2026-09-08", "000001", "sell", 14.0, 1500, key="close"),
        ]
    )

    assert len(trips) == 1
    assert trips[0].buy_date == date(2026, 9, 1)
    assert trips[0].buy_price == 11.0
    assert trips[0].sell_price == 13.75
    assert trips[0].sell_date == date(2026, 9, 8)
    assert trips[0].sell_event_key == "close"


def test_rebuy_after_close_starts_a_new_round_trip_and_skipped_rows_are_ignored() -> None:
    trips = closed_round_trips(
        [
            _event("2026-09-01", "000001", "buy", 10.0, 100),
            _event("2026-09-05", "000001", "buy", 0, 0, status="skipped"),
            _event("2026-09-08", "000001", "sell", 11.0, 100),
            _event("2026-09-08", "000001", "buy", 11.2, 200),
            _event("2026-09-10", "000001", "sell", 12.0, 200),
        ]
    )

    assert [(t.buy_date.day, t.buy_price, t.sell_date.day) for t in trips] == [(1, 10.0, 8), (8, 11.2, 10)]


def test_interval_high_low_includes_both_buy_and_sell_days() -> None:
    bars = _bars(
        [
            ("2026-08-29", 99.0, 1.0),
            ("2026-09-01", 13.20, 12.50),
            ("2026-09-02", 12.90, 12.10),
            ("2026-09-03", 12.70, 11.63),
            ("2026-09-04", 50.0, 0.5),
        ]
    )

    result = interval_high_low(bars, date(2026, 9, 1), date(2026, 9, 3))

    assert result is not None
    assert (result.high, result.high_date) == (13.20, date(2026, 9, 1))
    assert (result.low, result.low_date) == (11.63, date(2026, 9, 3))


def test_interval_high_low_skips_nan_and_returns_none_without_bars() -> None:
    bars = _bars([("2026-09-01", float("nan"), 10.0), ("2026-09-02", 11.0, float("nan"))])
    result = interval_high_low(bars, date(2026, 9, 1), date(2026, 9, 2))
    assert result is not None
    assert (result.high, result.low) == (11.0, 10.0)

    assert interval_high_low(None, date(2026, 9, 1), date(2026, 9, 2)) is None
    assert interval_high_low(bars, date(2026, 10, 1), date(2026, 10, 2)) is None
    assert interval_high_low(pd.DataFrame({"日期": ["2026-09-01"]}), date(2026, 9, 1), date(2026, 9, 1)) is None


def test_interval_high_low_accepts_english_columns() -> None:
    bars = pd.DataFrame({"date": ["2026-09-01", "2026-09-02"], "high": [10.0, 10.5], "low": [9.5, 9.8]})
    result = interval_high_low(bars, date(2026, 9, 1), date(2026, 9, 2))
    assert result is not None
    assert (result.high, result.low) == (10.5, 9.5)


def test_payload_marks_missing_interval_for_retry() -> None:
    trip = closed_round_trips(
        [_event("2026-09-01", "000001", "buy", 10.0, 100), _event("2026-09-02", "000001", "sell", 9.0, 100)]
    )[0]
    pending = round_trip_payload(trip, None, "tickflow")

    assert pending["interval_high"] is None and pending["bar_source"] == ""
    assert needs_round_trip_payload({"status": "filled"})
    assert needs_round_trip_payload({"round_trip": pending})
    done = round_trip_payload(
        trip, interval_high_low(_bars([("2026-09-01", 10.2, 9.1)]), trip.buy_date, trip.sell_date), "tickflow"
    )
    assert done["interval_low"] == 9.1 and done["interval_basis"] == "daily_unadjusted_inclusive"
    assert not needs_round_trip_payload({"round_trip": done})


def test_stored_round_trips_reads_sell_payloads_newest_first_and_skips_incomplete() -> None:
    def _sell(day: str, code: str, trip: object) -> dict:
        return {**_event(day, code, "sell", 11.0, 100), "payload": {"status": "filled", "round_trip": trip}}

    trip = {"code": "", "buy_date": "2026-09-01", "buy_price": 10.0, "sell_price": 11.0}
    events = [
        _sell("2026-09-10", "000001", {**trip, "code": "000001", "sell_date": "2026-09-10"}),
        _sell("2026-09-12", "000002", {**trip, "code": "000002", "sell_date": "2026-09-12"}),
        _sell("2026-09-12", "000003", {**trip, "code": "000003", "sell_date": "2026-09-12", "buy_price": None}),
        _sell("2026-09-13", "000004", "not-a-dict"),
        {**_event("2026-09-01", "000001", "buy", 10.0, 100), "payload": {"round_trip": {**trip, "sell_date": "x"}}},
    ]
    assert [row["code"] for row in stored_round_trips(events)] == ["000002", "000001"]
