from __future__ import annotations

import pandas as pd

from core.sector_rotation import analyze_sector_rotation


def _healthy_member_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=60)
    close = pd.Series([10.0 + i * 0.1 for i in range(60)])
    volume = pd.Series([1_000_000.0] * 60)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )


def test_analyze_sector_rotation_marks_healthy_mainline() -> None:
    df_map = {code: _healthy_member_frame() for code in ("000001", "000002", "000003")}
    sector_map = {code: "银行" for code in df_map}

    result = analyze_sector_rotation(df_map, sector_map)

    info = result["state_map"]["银行"]
    assert info["state"] == "HEALTHY_MAINLINE"
    assert result["counts"]["HEALTHY_MAINLINE"] == 1
    assert info["above_ma50_pct"] == 100.0
    assert result["headline"] == "分歧0 | 健康1 | 高潮0 | 退潮0 | 中性0"
    assert result["overview_lines"]


def test_analyze_sector_rotation_ignores_old_history_prefix() -> None:
    prefix_dates = pd.bdate_range("2025-01-01", periods=80)
    noisy_prefix = pd.DataFrame(
        {
            "date": prefix_dates,
            "open": [40.0] * 80,
            "high": [42.0] * 80,
            "low": [35.0] * 80,
            "close": [40.0 - i * 0.2 for i in range(80)],
            "volume": [3_000_000.0] * 80,
            "amount": [120_000_000.0] * 80,
        }
    )
    current = _healthy_member_frame()
    df_map = {code: pd.concat([noisy_prefix, current], ignore_index=True) for code in ("000001", "000002", "000003")}
    sector_map = {code: "银行" for code in df_map}

    result = analyze_sector_rotation(df_map, sector_map)

    assert result["state_map"]["银行"]["state"] == "HEALTHY_MAINLINE"
