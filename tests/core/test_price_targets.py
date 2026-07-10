"""Tests for core.price_targets: measured move / prior high / ATR multiple target prices."""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.price_targets import calc_atr, compute_price_targets


def test_calc_atr_returns_none_when_insufficient_history() -> None:
    high = pd.Series([10.0, 10.5, 11.0])
    low = pd.Series([9.5, 9.8, 10.2])
    close = pd.Series([9.8, 10.2, 10.8])

    assert calc_atr(high, low, close, period=14) is None


def test_calc_atr_matches_manual_true_range_average() -> None:
    close = pd.Series([10.0] * 20, dtype=float)
    high = close + 1.0
    low = close - 1.0

    atr = calc_atr(high, low, close, period=14)

    assert atr == 2.0  # 每日 high-low=2.0，且无跳空，TR恒等于2.0


def test_measured_move_requires_breakout_above_box_high() -> None:
    # 箱体高点=12（不含最后一天），最后一天收盘=11，尚未突破箱体上沿
    close = pd.Series([10.0, 11.0, 12.0, 10.5, 11.0])
    high = close + 0.2
    low = close - 0.2

    targets = compute_price_targets(close, high, low, box_lookback_days=5, prior_high_window=5)

    assert targets is not None
    assert targets.measured_move is None


def test_measured_move_fires_after_breakout() -> None:
    # 箱体(不含突破日)区间 [10, 12]（高度2），突破日收盘=13 > 箱体高点12
    close = pd.Series([10.0, 11.0, 12.0, 10.5, 13.0])
    high = close + 0.2
    low = close - 0.2

    targets = compute_price_targets(close, high, low, box_lookback_days=5, prior_high_window=5)

    assert targets is not None
    assert targets.measured_move == 14.0  # box_high(12) + box_height(2)


def test_prior_high_is_none_when_price_already_above_history() -> None:
    close = pd.Series([10.0, 10.5, 10.2, 10.8, 11.5])
    high = close + 0.1
    low = close - 0.1

    targets = compute_price_targets(close, high, low, box_lookback_days=5, prior_high_window=5)

    assert targets is not None
    assert targets.prior_high is None  # 现价 11.5 已经是历史最高，没有更高的前高可参考


def test_conservative_and_aggressive_pick_min_and_max_of_available_targets() -> None:
    rng = np.random.default_rng(7)
    base = 10.0 + np.cumsum(rng.normal(0, 0.05, 300))
    close = pd.Series(base)
    high = close + 0.3
    low = close - 0.3

    targets = compute_price_targets(close, high, low)

    assert targets is not None
    candidates = [v for v in (targets.measured_move, targets.prior_high, targets.atr_multiple) if v is not None]
    if candidates:
        assert targets.conservative == min(candidates)
        assert targets.aggressive == max(candidates)


def test_compute_price_targets_returns_none_for_empty_close() -> None:
    empty = pd.Series([], dtype=float)

    assert compute_price_targets(empty, empty, empty) is None
