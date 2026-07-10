from __future__ import annotations

import numpy as np
import pandas as pd

from core.wyckoff_engine import FunnelConfig
from core.wyckoff_v2_structure import detect_structure_triggers, identify_trading_range


def _range_df(n: int = 120) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=n)
    x = np.linspace(0, 10 * np.pi, n)
    close = 11.0 + 0.9 * np.sin(x)
    open_ = close * 0.998
    high = close + 0.22
    low = close - 0.22
    volume = np.full(n, 1_000_000.0)
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "pct_chg": pd.Series(close).pct_change().fillna(0.0) * 100.0,
        }
    )


def test_identify_trading_range_from_repeated_swings():
    df = _range_df()
    cfg = FunnelConfig()

    tr = identify_trading_range(df, cfg, exclude_last=0)

    assert tr is not None
    assert 9.5 <= tr.support <= 10.5
    assert 11.5 <= tr.resistance <= 12.5
    assert tr.support_tests >= 2
    assert tr.resistance_tests >= 2


def test_structure_spring_uses_prior_trading_range():
    df = _range_df()
    # Last bar pierces the already visible support and recovers above it.
    df.loc[df.index[-1], ["open", "high", "low", "close", "volume", "pct_chg"]] = [
        10.0,
        10.7,
        9.55,
        10.45,
        1_700_000.0,
        4.0,
    ]
    cfg = FunnelConfig()
    cfg.spring_vol_ratio = 1.0

    result = detect_structure_triggers(["000001"], {"000001": df}, cfg)

    assert result.trading_ranges["000001"].support < 10.5
    assert result.triggers["spring"]
    assert result.stage_map["000001"] == "Accum_C"


def test_structure_sos_uses_dynamic_resistance():
    df = _range_df()
    df.loc[df.index[-1], ["open", "high", "low", "close", "volume", "pct_chg"]] = [
        11.6,
        12.9,
        11.5,
        12.65,
        3_000_000.0,
        7.0,
    ]
    cfg = FunnelConfig()
    cfg.sos_pct_min = 5.0
    cfg.sos_vol_ratio = 2.0

    result = detect_structure_triggers(["000001"], {"000001": df}, cfg)

    assert result.triggers["sos"]
    assert result.stage_map["000001"] == "Markup"
