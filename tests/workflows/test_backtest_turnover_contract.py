from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from workflows import backtest_turnover as turnover
from workflows.backtest_data import load_snapshot_hist_map
from workflows.backtest_snapshot_fetch import _frame_from_tickflow_batch


def test_batch_missing_turnover_stays_missing_not_zero():
    raw = pd.DataFrame(
        {
            "date": ["2026-09-30"],
            "open": [10],
            "high": [11],
            "low": [9],
            "close": [10],
            "prev_close": [10],
            "volume": [10],
            "amount": [10000],
        }
    )
    frame, error = _frame_from_tickflow_batch("000001", raw, "2026-09-01", "2026-09-30")
    assert error is None
    assert frame["turnover"].isna().all()


def test_snapshot_csv_preserves_turnover_and_source(tmp_path):
    pd.DataFrame(
        {
            "symbol": ["000001"],
            "date": ["2026-09-30"],
            "close": [10],
            "turnover": [2.5],
            "turnover_source": ["tushare.daily_basic"],
        }
    ).to_csv(tmp_path / "hist_full.csv.gz", index=False, compression="gzip")
    frames, rows = load_snapshot_hist_map(tmp_path)
    assert rows == 1
    assert frames["000001"].iloc[0]["turnover"] == 2.5
    assert frames["000001"].iloc[0]["turnover_source"] == "tushare.daily_basic"


def test_pit_join_is_date_keyed_and_preserves_native(monkeypatch):
    calls = []

    def fetch(**kw):
        calls.append(kw["trade_date"])
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [kw["trade_date"]],
                "turnover_rate": [2.0 if kw["trade_date"] == "20260929" else 3.0],
            }
        )

    monkeypatch.setattr(turnover, "get_pro", lambda: SimpleNamespace(daily_basic=fetch))
    frame = pd.DataFrame(
        {"symbol": ["000001", "000001"], "date": ["2026-09-29", "2026-09-30"], "turnover": [1.0, None]}
    )
    meta = turnover.attach_snapshot_pit_turnover([frame])
    assert list(frame.turnover) == [1, 3]
    assert list(frame.turnover_source) == ["native", "tushare.daily_basic"]
    assert calls == ["20260929", "20260930"]
    assert meta["contract"] == turnover.TURNOVER_CONTRACT


def test_pit_query_rejects_wrong_date(monkeypatch):
    monkeypatch.setattr(
        turnover,
        "get_pro",
        lambda: SimpleNamespace(
            daily_basic=lambda **kw: pd.DataFrame(
                {"ts_code": ["000001.SZ"], "trade_date": ["20261009"], "turnover_rate": [1.0]}
            )
        ),
    )
    frame = pd.DataFrame({"symbol": ["000001"], "date": ["2026-09-30"]})
    with pytest.raises(ValueError, match="非请求日"):
        turnover.attach_snapshot_pit_turnover([frame])


def test_strict_guard_rejects_legacy_even_with_zero_values():
    frames = {"000001": pd.DataFrame({"date": ["2026-09-30"], "turnover": [0]})}
    with pytest.raises(ValueError, match="旧快照"):
        turnover.validate_snapshot_turnover(frames, {}, date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.parametrize("value", [None, float("inf"), -1])
def test_strict_guard_refuses_invalid_values(value):
    frames = {"000001": pd.DataFrame({"date": ["2026-09-30"], "turnover": [value]})}
    meta = {"turnover_pit": {"contract": turnover.TURNOVER_CONTRACT}}
    with pytest.raises(ValueError, match="不足95%"):
        turnover.validate_snapshot_turnover(frames, meta, date(2026, 9, 1), date(2026, 9, 30))


def test_strict_guard_accepts_real_zero_and_excludes_warmup():
    frames = {"000001": pd.DataFrame({"date": ["2026-08-01", "2026-09-30"], "turnover": [None, 0]})}
    meta = {"turnover_pit": {"contract": turnover.TURNOVER_CONTRACT}}
    assert turnover.validate_snapshot_turnover(frames, meta, date(2026, 9, 1), date(2026, 9, 30))["min_coverage"] == 1
