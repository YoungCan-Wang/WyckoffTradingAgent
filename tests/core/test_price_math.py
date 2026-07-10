from __future__ import annotations

import pandas as pd

from core._price_math import (
    clamp,
    day_close_pos,
    dist_pct,
    drawdown_pct,
    numeric_column,
    range_pos,
    ret_pct,
    to_numeric,
    upper_shadow_pct,
    vol_ratio,
)


def test_clamp_bounds_value_within_range():
    assert clamp(0.5) == 0.5
    assert clamp(-1.0) == 0.0
    assert clamp(2.0) == 1.0
    assert clamp(15.0, low=10.0, high=20.0) == 15.0
    assert clamp(5.0, low=10.0, high=20.0) == 10.0


def test_range_pos_computes_relative_position():
    assert range_pos(15.0, 10.0, 20.0) == 0.5
    assert range_pos(10.0, 10.0, 20.0) == 0.0
    assert range_pos(20.0, 10.0, 20.0) == 1.0


def test_range_pos_returns_midpoint_when_range_is_empty_or_inverted():
    assert range_pos(5.0, 10.0, 10.0) == 0.5
    assert range_pos(5.0, 20.0, 10.0) == 0.5


def test_to_numeric_coerces_invalid_values_to_nan():
    series = pd.Series(["1.5", "bad", "3"])
    result = to_numeric(series)
    assert result.iloc[0] == 1.5
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == 3.0


def test_numeric_column_missing_column_returns_empty_series():
    df = pd.DataFrame({"close": [1, 2, 3]})
    result = numeric_column(df, "missing")
    assert result.empty


def test_numeric_column_drops_na_by_default():
    df = pd.DataFrame({"close": ["1", "bad", "3"]})
    result = numeric_column(df, "close")
    assert list(result) == [1.0, 3.0]


def test_numeric_column_keeps_na_when_dropna_false():
    df = pd.DataFrame({"close": ["1", "bad", "3"]})
    result = numeric_column(df, "close", dropna=False)
    assert len(result) == 3
    assert pd.isna(result.iloc[1])


def test_ret_pct_computes_percentage_return_over_lookback():
    close = pd.Series([100.0, 102.0, 105.0, 110.0])
    assert ret_pct(close, 1) == (110.0 / 105.0 - 1.0) * 100.0
    assert round(ret_pct(close, 3), 6) == 10.0


def test_ret_pct_returns_zero_when_series_too_short():
    close = pd.Series([100.0, 102.0])
    assert ret_pct(close, 5) == 0.0


def test_ret_pct_returns_zero_when_start_value_non_positive():
    close = pd.Series([0.0, 102.0])
    assert ret_pct(close, 1) == 0.0


def test_dist_pct_computes_distance_from_base():
    assert round(dist_pct(110.0, 100.0), 6) == 10.0
    assert round(dist_pct(90.0, 100.0), 6) == -10.0


def test_dist_pct_returns_zero_when_base_non_positive():
    assert dist_pct(110.0, 0.0) == 0.0
    assert dist_pct(110.0, -5.0) == 0.0


def test_drawdown_pct_computes_negative_percentage_from_recent_high():
    close = pd.Series([100.0, 120.0, 90.0])
    assert drawdown_pct(close, 3) == (90.0 / 120.0 - 1.0) * -100.0


def test_drawdown_pct_returns_zero_when_series_empty_or_high_non_positive():
    assert drawdown_pct(pd.Series([], dtype=float), 5) == 0.0
    assert drawdown_pct(pd.Series([0.0, 0.0]), 2) == 0.0


def test_upper_shadow_pct_measures_wick_above_body():
    df = pd.DataFrame({"open": [10.0], "high": [12.0], "close": [11.0]})
    result = upper_shadow_pct(df, df["open"], df["high"], df["close"])
    assert round(result, 4) == round((12.0 - 11.0) / 11.0 * 100.0, 4)


def test_upper_shadow_pct_returns_zero_when_high_or_close_empty():
    empty = pd.Series([], dtype=float)
    non_empty = pd.Series([1.0])
    assert upper_shadow_pct(pd.DataFrame(), empty, non_empty, non_empty) == 0.0
    assert upper_shadow_pct(pd.DataFrame(), non_empty, non_empty, empty) == 0.0


def test_day_close_pos_uses_last_row_by_default():
    close = pd.Series([10.0, 15.0])
    high = pd.Series([12.0, 20.0])
    low = pd.Series([8.0, 10.0])
    assert day_close_pos(close, high, low) == range_pos(15.0, 10.0, 20.0)


def test_day_close_pos_uses_tail_min_max_when_use_tail_true():
    close = pd.Series([10.0, 15.0])
    high = pd.Series([12.0, 20.0])
    low = pd.Series([8.0, 10.0])
    assert day_close_pos(close, high, low, use_tail=True) == range_pos(15.0, 10.0, 20.0)


def test_day_close_pos_returns_midpoint_when_high_or_low_empty():
    empty = pd.Series([], dtype=float)
    non_empty = pd.Series([1.0])
    assert day_close_pos(non_empty, empty, non_empty) == 0.5
    assert day_close_pos(non_empty, non_empty, empty) == 0.5


def test_vol_ratio_compares_recent_five_to_trailing_twenty():
    volume = pd.Series([100.0] * 15 + [200.0] * 5)
    base = pd.Series([100.0] * 15 + [200.0] * 5).tail(20).mean()
    expected = pd.Series([200.0] * 5).mean() / base
    assert round(vol_ratio(volume), 6) == round(expected, 6)


def test_vol_ratio_returns_one_when_series_shorter_than_twenty():
    volume = pd.Series([100.0] * 10)
    assert vol_ratio(volume) == 1.0


def test_vol_ratio_returns_one_when_base_non_positive():
    volume = pd.Series([0.0] * 20)
    assert vol_ratio(volume) == 1.0
