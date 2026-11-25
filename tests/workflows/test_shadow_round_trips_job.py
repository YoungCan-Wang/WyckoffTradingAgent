"""盘后补写清仓回合区间高低：只补缺的、保留原 payload、拉不到日线下次再试。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from workflows import shadow_round_trips_job as job


def _event(day: str, code: str, action: str, price: float, qty: int, payload: dict | None = None) -> dict:
    return {
        "event_key": f"{day}:{action}:{code}",
        "as_of": day,
        "code": code,
        "name": code,
        "event_type": action,
        "price": price,
        "qty": qty,
        "payload": {"status": "filled", "plan_key": "pk", **(payload or {})},
    }


def _bars(code: str, start: date, end: date) -> tuple[pd.DataFrame | None, str]:
    if code == "000003":
        return None, ""
    frame = pd.DataFrame({"日期": ["2026-09-01", "2026-09-02"], "最高": [11.0, 12.0], "最低": [9.0, 9.5]})
    return frame, "tickflow"


def test_annotates_closed_trips_only_and_keeps_existing_payload_keys() -> None:
    done = {"round_trip": {"interval_high": 1.0, "interval_low": 0.5}}
    events = [
        _event("2026-09-01", "000001", "buy", 10.0, 100),
        _event("2026-09-02", "000001", "sell", 11.0, 100),
        _event("2026-09-01", "000002", "buy", 10.0, 100),
        _event("2026-09-02", "000002", "sell", 11.0, 100, done),
        _event("2026-09-01", "000003", "buy", 10.0, 100),
        _event("2026-09-02", "000003", "sell", 11.0, 100),
        _event("2026-09-01", "000004", "buy", 10.0, 100),
    ]
    fetched: list[str] = []
    writes: dict[str, dict] = {}

    def fetch(code: str, start: date, end: date):
        fetched.append(code)
        return _bars(code, start, end)

    def write(key: str, patch: dict, current: dict) -> bool:
        writes[key] = {**current, **patch}
        return True

    stats = job.annotate_closed_round_trips(events, fetch_bars=fetch, write_payload=write)

    assert stats == {"closed": 3, "written": 2, "missing_bars": 1, "unmatched": 0}
    assert fetched == ["000001", "000003"]
    first = writes["2026-09-02:sell:000001"]
    assert first["plan_key"] == "pk" and first["status"] == "filled"
    assert first["round_trip"]["buy_price"] == 10.0 and first["round_trip"]["sell_price"] == 11.0
    assert (first["round_trip"]["interval_low"], first["round_trip"]["interval_high"]) == (9.0, 12.0)
    assert writes["2026-09-02:sell:000003"]["round_trip"]["interval_high"] is None


def test_update_that_matches_no_row_is_not_counted_as_written() -> None:
    events = [_event("2026-09-01", "000001", "buy", 10.0, 100), _event("2026-09-02", "000001", "sell", 11.0, 100)]

    stats = job.annotate_closed_round_trips(events, fetch_bars=_bars, write_payload=lambda *_a: False)

    assert stats == {"closed": 1, "written": 0, "missing_bars": 0, "unmatched": 1}


def test_unadjusted_fetch_never_uses_the_qfq_fallback_chain(monkeypatch) -> None:
    import integrations.data_source_akshare as ak
    import integrations.data_source_tickflow as tf

    calls: list[tuple[str, str, str, str]] = []

    def tickflow(symbol: str, start: str, end: str, adjust: str):
        calls.append(("tickflow", start, end, adjust))
        raise RuntimeError("down")

    def akshare(symbol: str, start: str, end: str, adjust: str):
        calls.append(("akshare", start, end, adjust))
        return pd.DataFrame({"日期": ["2026-09-01"], "最高": [1.0], "最低": [0.9]})

    monkeypatch.setattr(tf, "fetch_stock_tickflow", tickflow)
    monkeypatch.setattr(ak, "fetch_stock_akshare", akshare)

    frame, source = job.fetch_unadjusted_daily("000001", date(2026, 9, 1), date(2026, 9, 3))

    assert source == "akshare" and frame is not None
    assert calls == [("tickflow", "20260901", "20260903", "none"), ("akshare", "20260901", "20260903", "")]
