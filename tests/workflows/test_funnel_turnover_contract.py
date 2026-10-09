from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from workflows import funnel_data
from workflows.funnel_data_quality import build_funnel_data_quality


def frame(values, volumes=None):
    df = pd.DataFrame(
        {"date": ["2026-09-29", "2026-09-30"], "volume": volumes or [10000.0, 20000.0], "turnover": values}
    )
    df.attrs["volume_unit"] = "shares"
    return df


def quality(df):
    return build_funnel_data_quality(
        ["000001"],
        {"000001": df},
        {"000001": 100},
        {},
        financial_requested=False,
        turnover_expected=True,
        expected_trade_date=date(2026, 9, 30),
    )


def test_native_turnover_is_not_overwritten(monkeypatch):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {"000001": 1e6})
    df = frame([4.0, 5.0])
    assert funnel_data._attach_turnover({"000001": df}) == 1.0
    assert list(df.turnover) == [4.0, 5.0]
    assert df.attrs["turnover_source"] == "native"


def test_partial_native_values_are_preserved_and_only_missing_is_derived(monkeypatch):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {"000001": 1e6})
    df = frame([4.0, None])
    assert funnel_data._attach_turnover({"000001": df}) == 1.0
    assert list(df.turnover) == [4.0, 2.0]
    assert df.attrs["turnover_source"] == "native+float_share_snapshot"


def test_target_trade_date_passes_to_share_query(monkeypatch):
    seen = []

    def fetch(**kw):
        seen.append(kw)
        return {"000001": 1e6}

    monkeypatch.setattr(funnel_data, "fetch_float_share_map", fetch)
    df = frame([None, None])
    assert funnel_data._attach_turnover({"000001": df}, as_of_date=date(2026, 9, 30)) == 1.0
    assert seen == [{"as_of_date": date(2026, 9, 30)}]
    assert df.attrs["turnover_share_asof"] == "2026-09-30"


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), -1])
def test_old_value_cannot_hide_invalid_latest_turnover(invalid):
    df = frame([2.0, invalid])
    result = quality(df)
    assert result["coverage"]["turnover"] == 0
    assert result["trade_readiness"] == "observe_only"
    assert "turnover_coverage<95%" in result["reasons"]


def test_descending_frame_counts_actual_latest_row():
    df = frame([None, 2.0]).iloc[::-1]
    assert quality(df)["coverage"]["turnover"] == 1


def test_latest_zero_turnover_is_valid_not_missing():
    assert quality(frame([2.0, 0.0]))["coverage"]["turnover"] == 1


@pytest.mark.parametrize("shares", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_share_denominator_cannot_create_coverage(monkeypatch, shares):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {"000001": shares})
    df = frame([None, None])
    assert funnel_data._attach_turnover({"000001": df}) == 0
    assert quality(df)["trade_readiness"] == "observe_only"


def test_missing_share_map_preserves_native_coverage(monkeypatch):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {})
    df = frame([1.0, 2.0])
    assert funnel_data._attach_turnover({"000001": df}) == 1.0
    assert quality(df)["coverage"]["turnover"] == 1


@pytest.mark.parametrize(
    "source,factor",
    [("tickflow_batch", 100), ("tickflow", 100), ("akshare", 100), ("efinance", 100), ("tushare", 1), ("baostock", 1)],
)
def test_known_source_units_are_converted_once(monkeypatch, source, factor):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {"000001": 1e9})
    df = frame([None, None], [50000, 100000])
    df.attrs = {"source": source}
    assert funnel_data._attach_turnover({"000001": df}) == 1
    assert list(df.turnover) == [0.005 * factor, 0.01 * factor]
    assert list(df.volume) == [50000, 100000]


def test_explicit_volume_unit_overrides_vendor_default(monkeypatch):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {"000001": 1e9})
    df = frame([None, None], [5000000, 10000000])
    df.attrs["source"] = "tickflow_batch"
    assert funnel_data._attach_turnover({"000001": df}) == 1
    assert list(df.turnover) == [0.5, 1.0]


def test_unknown_volume_unit_cannot_fake_turnover_coverage(monkeypatch):
    monkeypatch.setattr(funnel_data, "fetch_float_share_map", lambda **kw: {"000001": 1e9})
    df = frame([None, None])
    df.attrs = {"source": "unknown"}
    assert funnel_data._attach_turnover({"000001": df}) == 0
    assert quality(df)["trade_readiness"] == "observe_only"
