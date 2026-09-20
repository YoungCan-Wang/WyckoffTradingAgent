from __future__ import annotations

import pytest

from core._price_math import fib_absorption_check, fib_retracement_levels


def test_fib_retracement_levels_normal():
    # Swing: 50 -> 100, span = 50
    levels = fib_retracement_levels(100.0, 50.0)
    assert len(levels) == 3
    assert pytest.approx(levels[0.382], 0.001) == 100.0 - 50.0 * 0.382  # 80.9
    assert pytest.approx(levels[0.500], 0.001) == 75.0
    assert pytest.approx(levels[0.618], 0.001) == 100.0 - 50.0 * 0.618  # 69.1


def test_fib_retracement_levels_invalid():
    assert fib_retracement_levels(50.0, 100.0) == {}
    assert fib_retracement_levels(100.0, 100.0) == {}
    assert fib_retracement_levels(100.0, 0.0) == {}
    assert fib_retracement_levels(100.0, -10.0) == {}


def test_fib_absorption_check_touch_and_hold():
    level_price = 75.0

    # Touched and closed above with heavy volume absorption
    touched, absorption = fib_absorption_check(
        curr_low=74.5,
        curr_close=75.5,
        price_level=level_price,
        curr_vol=2000.0,
        avg_vol=1000.0,
        min_vol_ratio=1.2,
    )
    assert touched is True
    assert absorption is True

    # Touched and closed above with dry/low volume (no absorption)
    touched, absorption = fib_absorption_check(
        curr_low=74.5,
        curr_close=75.5,
        price_level=level_price,
        curr_vol=800.0,
        avg_vol=1000.0,
        min_vol_ratio=1.0,
    )
    assert touched is True
    assert absorption is False


def test_fib_absorption_check_breakdown_or_untouched():
    level_price = 75.0

    # Touched but broke down (closed below)
    touched, absorption = fib_absorption_check(
        curr_low=73.0,
        curr_close=74.0,
        price_level=level_price,
        curr_vol=2000.0,
        avg_vol=1000.0,
    )
    assert touched is False
    assert absorption is False

    # Untouched (low stayed above)
    touched, absorption = fib_absorption_check(
        curr_low=76.0,
        curr_close=77.0,
        price_level=level_price,
        curr_vol=2000.0,
        avg_vol=1000.0,
    )
    assert touched is False
    assert absorption is False


def test_fib_absorption_check_invalid_inputs():
    touched, absorption = fib_absorption_check(
        curr_low=10.0,
        curr_close=0.0,
        price_level=10.0,
        curr_vol=100.0,
        avg_vol=100.0,
    )
    assert touched is False
    assert absorption is False

    touched, absorption = fib_absorption_check(
        curr_low=10.0,
        curr_close=10.0,
        price_level=0.0,
        curr_vol=100.0,
        avg_vol=100.0,
    )
    assert touched is False
    assert absorption is False
