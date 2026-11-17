"""Unit tests for main-funnel structural guards (variants S/T/U/V)."""

from __future__ import annotations

import pandas as pd

from core.layer2_strength import _diagnose_dry_vol, dry_volume_channel_ok
from core.wyckoff_engine import FunnelConfig, layer2_strength_detailed, layer4_triggers


def _make_ohlcv_df(
    closes: list[float],
    volumes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    n = len(closes)
    dates = pd.bdate_range("2025-01-02", periods=n)
    o = opens if opens is not None else list(closes)
    h = highs if highs is not None else [c * 1.01 for c in closes]
    l_vals = lows if lows is not None else [c * 0.99 for c in closes]
    pct = pd.Series(closes).pct_change().fillna(0.0) * 100.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": o,
            "high": h,
            "low": l_vals,
            "close": closes,
            "volume": volumes,
            "amount": [v * c for v, c in zip(volumes, closes, strict=True)],
            "pct_chg": pct.tolist(),
            "turnover": [3.0] * n,
        }
    )


def test_dry_vol_breakdown_guard_blocks_falling_knife_but_keeps_tight_base() -> None:
    base_df = _make_ohlcv_df([10.0] * 260, [1000.0] * 250 + [300.0] * 10)
    knife_closes = [10.0] * 257 + [9.8, 9.4, 8.7]
    knife_vols = [1000.0] * 250 + [300.0] * 7 + [1000.0, 1500.0, 2200.0]
    knife_df = _make_ohlcv_df(knife_closes, knife_vols)

    cfg_off = FunnelConfig(dry_vol_breakdown_guard_enabled=False)
    assert dry_volume_channel_ok(cfg_off, df_sorted=base_df, close=base_df["close"], last_close=10.0) is True
    assert dry_volume_channel_ok(cfg_off, df_sorted=knife_df, close=knife_df["close"], last_close=8.7) is True

    cfg_on = FunnelConfig(dry_vol_breakdown_guard_enabled=True)
    assert dry_volume_channel_ok(cfg_on, df_sorted=base_df, close=base_df["close"], last_close=10.0) is True
    assert dry_volume_channel_ok(cfg_on, df_sorted=knife_df, close=knife_df["close"], last_close=8.7) is False

    gap, reasons = _diagnose_dry_vol(cfg_on, knife_df, knife_df["close"], 8.7)
    assert gap > 0
    assert any("跌破MA20支撑超限" in reason for reason in reasons)


def test_l2_persistence_guard_filters_single_day_impulse_and_keeps_shakeout_reclaim() -> None:
    closes = [11.0 - 0.01 * i for i in range(80)]
    df_map = {
        "300001": _make_ohlcv_df(closes, [1000.0] * 77 + [100.0, 100.0, 100.0]),
        "300002": _make_ohlcv_df(closes, [1000.0] * 77 + [100.0, 5000.0, 100.0]),
        "300003": _make_ohlcv_df(closes, [1000.0] * 77 + [5000.0, 5000.0, 100.0]),
    }
    cfg = FunnelConfig(
        ma_short=5,
        ma_long=20,
        enable_rs_filter=False,
        enable_rps_filter=False,
        enable_ambush_channel=False,
        enable_accumulation_channel=False,
        dry_vol_ref_window=60,
        dry_vol_lookback=1,
        dry_vol_quantile=0.20,
        l2_persistence_guard_enabled=True,
        l2_persistence_window_days=3,
    )
    rejections: dict[str, str] = {}
    passed, channel_map, _ = layer2_strength_detailed(
        ["300001", "300002", "300003"], df_map, None, cfg, rejections=rejections
    )
    assert passed == ["300001", "300002"]
    assert set(channel_map) == {"300001", "300002"}
    assert "300003" not in passed
    assert "L2跨日持续性不足" in rejections["300003"]


def _overhang_evr_df(last_close: float) -> pd.DataFrame:
    n = 210
    closes = [10.0 + (0.01 * (i - 150) if i >= 150 else 0.0) for i in range(n)]
    opens = list(closes)
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    vols = [1000.0] * n
    # Index 205 (T-4): high-volume upper-shadow climax distribution bar (climax high = 11.8)
    opens[205], closes[205], lows[205], highs[205], vols[205] = 11.0, 11.0, 10.9, 11.8, 3500.0
    for i in (206, 207):
        opens[i] = closes[i] = 11.0
        highs[i], lows[i] = 11.1, 10.9
    # Index 208 (idx=-2): high-volume tight-range EVR candidate bar
    opens[208], closes[208], lows[208], highs[208], vols[208] = 11.0, 11.05, 10.95, 11.15, 3000.0
    # Index 209 (idx=-1): confirmation bar holding event low
    opens[209], closes[209], lows[209], highs[209] = 11.05, last_close, 11.0, max(last_close * 1.01, 11.2)
    return _make_ohlcv_df(closes, vols, opens=opens, highs=highs, lows=lows)


def test_supply_overhang_guard_vetoes_unreclaimed_climax_wick_and_lifts_after_reclaim() -> None:
    blocked_df = _overhang_evr_df(last_close=11.15)
    reclaimed_df = _overhang_evr_df(last_close=11.90)

    cfg_off = FunnelConfig(supply_overhang_guard_enabled=False, enable_rps_filter=False)
    triggers_off = layer4_triggers(["000001"], {"000001": blocked_df}, cfg_off, channel_map={"000001": "吸筹通道"})
    assert [code for code, _ in triggers_off["evr"]] == ["000001"]

    cfg_on = FunnelConfig(supply_overhang_guard_enabled=True, enable_rps_filter=False)
    triggers_blocked = layer4_triggers(["000001"], {"000001": blocked_df}, cfg_on, channel_map={"000001": "吸筹通道"})
    assert triggers_blocked["evr"] == []

    triggers_reclaimed = layer4_triggers(
        ["000001"], {"000001": reclaimed_df}, cfg_on, channel_map={"000001": "吸筹通道"}
    )
    assert [code for code, _ in triggers_reclaimed["evr"]] == ["000001"]
