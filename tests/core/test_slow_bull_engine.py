from __future__ import annotations

import math

import numpy as np
import pandas as pd

from core.slow_bull_engine import (
    SlowBullConfig,
    annualize_roe,
    detect_tech_rotation_warning,
    diagnose_slow_bull_symbol,
    evaluate_slow_bull_moat,
    ols_signed_log_r2,
    scan_slow_bull_candidates,
)


def _dates(n: int) -> list[str]:
    return pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d").tolist()


def _frame_from_closes(
    closes: list[float],
    volumes: list[float] | None = None,
    *,
    amount_yuan: float = 3.0e8,
    turnover_pct: float = 1.2,
    amp_ratio: float = 0.008,
) -> pd.DataFrame:
    n = len(closes)
    vols = volumes if volumes is not None else [1_000_000.0] * n
    rows = []
    for idx, close in enumerate(closes):
        prev = closes[idx - 1] if idx > 0 else close
        high = max(close, prev) * (1.0 + amp_ratio * 0.5)
        low = min(close, prev) * (1.0 - amp_ratio * 0.5)
        pct = (close / prev - 1.0) * 100.0 if idx > 0 and prev > 0 else 0.0
        rows.append(
            {
                "date": _dates(n)[idx],
                "open": prev,
                "high": high,
                "low": low,
                "close": close,
                "prev_close": prev,
                "volume": vols[idx],
                "amount": amount_yuan,
                "pct_chg": pct,
                "turnover": turnover_pct,
            }
        )
    return pd.DataFrame(rows)


def _stage_a_fixture() -> tuple[pd.DataFrame, pd.DataFrame]:
    closes = [10.0 + 0.12 * math.sin(i / 4.0) for i in range(126)]
    for _ in range(14):
        closes.append(closes[-1] * 1.012)
    vols = [1_000_000.0] * 130 + [1_450_000.0] * 10
    stock_df = _frame_from_closes(closes, vols)
    bm_closes = [4000.0] * 130 + [4000.0 - 5.0 * k for k in range(10)]
    csi_df = _frame_from_closes(bm_closes)
    return stock_df, csi_df


def _stage_b_fixture(*, last_volume: float = 1_100_000.0, pullback: bool = True) -> pd.DataFrame:
    closes = [10.0] * 50
    for idx in range(39):
        step_ret = 0.007 if idx % 3 != 2 else 0.002
        closes.append(closes[-1] * (1.0 + step_ret))
    ma5_approx = sum(closes[-4:]) / 4.0
    if pullback:
        closes.append(ma5_approx * 0.998)
    else:
        closes.append(closes[-1] * 1.032)
    vols = [2_000_000.0] * 89 + [last_volume]
    return _frame_from_closes(closes, vols, amp_ratio=0.01)


def test_ols_signed_log_r2_uptrend_downtrend_and_flat() -> None:
    up = [10.0 * (1.01**k) for k in range(40)]
    down = [10.0 * (0.99**k) for k in range(40)]
    flat = [10.0] * 40

    r2_up, slope_up = ols_signed_log_r2(up)
    r2_down, slope_down = ols_signed_log_r2(down)
    r2_flat, slope_flat = ols_signed_log_r2(flat)

    assert r2_up > 0.99 and slope_up > 0
    assert r2_down < -0.99 and slope_down < 0
    assert r2_flat == 0.0 and slope_flat == 0.0


def test_annualize_roe_scales_quarters() -> None:
    assert annualize_roe(2.0, "2026-03-31") == 8.0
    assert annualize_roe(3.5, "2026-06-30") == 7.0
    assert round(annualize_roe(6.0, "2026-09-30") or 0.0, 2) == 8.0
    assert annualize_roe(6.0, "2025-12-31") == 6.0


def test_moat_financial_sector_exemption_and_cycle_bypass() -> None:
    df = _stage_b_fixture()

    bank_ok, bank_reasons, _ = evaluate_slow_bull_moat(
        df,
        market_cap_yi=5000.0,
        dv_ttm_pct=5.2,
        sector="银行",
        financial={
            "roe": 8.2,
            "period_end": "2026-09-30",
            "debt_to_asset_ratio": 91.5,
            "operating_cash_to_revenue": -5.0,
        },
    )
    assert bank_ok and bank_reasons == []

    high_debt_ok, high_debt_reasons, _ = evaluate_slow_bull_moat(
        df,
        market_cap_yi=300.0,
        sector="化工",
        financial={
            "roe": 9.0,
            "period_end": "2026-12-31",
            "debt_to_asset_ratio": 85.0,
            "operating_cash_to_revenue": 8.0,
        },
    )
    assert not high_debt_ok and any("资产负债率" in r for r in high_debt_reasons)

    fin_cycle = {
        "roe": -1.5,
        "period_end": "2026-09-30",
        "debt_to_asset_ratio": 55.0,
        "operating_cash_to_revenue": 14.0,
    }
    strict_ok, _, _ = evaluate_slow_bull_moat(df, market_cap_yi=1000.0, sector="农林牧渔", financial=fin_cycle)
    bypass_ok, _, moat_meta = evaluate_slow_bull_moat(
        df,
        market_cap_yi=1000.0,
        sector="农林牧渔",
        financial=fin_cycle,
        cfg=SlowBullConfig(allow_cycle_cashflow_bypass=True),
    )
    assert not strict_ok
    assert bypass_ok and moat_meta["cycle_cashflow_bypass"] is True

    small_ok, _, _ = evaluate_slow_bull_moat(df, market_cap_yi=80.0, sector="食品饮料")
    assert not small_ok


def test_stage_a_strict_accepts_quiet_emergence_and_rejects_spiky_candle() -> None:
    stock_df, csi_df = _stage_a_fixture()
    fin = {"roe": 12.0, "period_end": "2026-09-30", "debt_to_asset_ratio": 45.0, "operating_cash_to_revenue": 15.0}

    passed = diagnose_slow_bull_symbol(
        "600887",
        stock_df,
        name="伊利样例",
        csi300_df=csi_df,
        market_cap_yi=1800.0,
        sector="食品饮料",
        financial=fin,
        rps50_current=75.0,
        rps50_prior_min=40.0,
    )
    assert passed["signal_type"] == "slow_bull_emerge"
    assert "slow_bull_emerge" in passed["matched_stages"]

    spiky_df = stock_df.copy()
    spiky_df.loc[len(spiky_df) - 2, "pct_chg"] = 4.2
    rejected = diagnose_slow_bull_symbol(
        "600887",
        spiky_df,
        name="脉冲样例",
        csi300_df=csi_df,
        market_cap_yi=1800.0,
        sector="食品饮料",
        financial=fin,
        rps50_current=75.0,
        rps50_prior_min=40.0,
    )
    assert "slow_bull_emerge" not in rejected["matched_stages"]


def test_stage_b_distinguishes_pullback_watch_and_rejects_pseudo_slow_bull() -> None:
    fin = {"roe": 14.0, "period_end": "2026-09-30", "debt_to_asset_ratio": 40.0, "operating_cash_to_revenue": 16.0}
    pullback = diagnose_slow_bull_symbol(
        "600887",
        _stage_b_fixture(last_volume=1_100_000.0, pullback=True),
        name="伊利样例",
        market_cap_yi=1800.0,
        sector="食品饮料",
        financial=fin,
    )
    assert pullback["signal_type"] == "slow_bull_pullback"

    watch = diagnose_slow_bull_symbol(
        "600887",
        _stage_b_fixture(last_volume=2_500_000.0, pullback=False),
        name="伊利样例",
        market_cap_yi=1800.0,
        sector="食品饮料",
        financial=fin,
    )
    assert watch["signal_type"] == "slow_bull_watch"

    pseudo_df = _stage_b_fixture(last_volume=1_100_000.0, pullback=True)
    pseudo_df.loc[len(pseudo_df) - 5, "pct_chg"] = 12.5
    pseudo = diagnose_slow_bull_symbol(
        "301101",
        pseudo_df,
        name="伪慢牛",
        market_cap_yi=220.0,
        sector="光学光电子",
        financial=fin,
    )
    assert pseudo["signal_type"] == "none"


def test_detect_tech_rotation_warning() -> None:
    csi = _frame_from_closes([4000.0] * 10 + [4000.0 * (1.0018**k) for k in range(25)])
    cyb = _frame_from_closes([2000.0] * 10 + [2000.0 * (1.0075**k) for k in range(25)])
    climate = detect_tech_rotation_warning(csi, cyb)
    assert climate["tech_rotation_warning"] is True
    assert climate["spread_20d_pct"] > 6.0


def test_scan_slow_bull_candidates_cross_sectional_counts() -> None:
    fin = {"roe": 12.0, "period_end": "2026-09-30", "debt_to_asset_ratio": 42.0, "operating_cash_to_revenue": 12.0}
    df_pullback = _stage_b_fixture(last_volume=1_100_000.0, pullback=True)
    df_watch = _stage_b_fixture(last_volume=2_400_000.0, pullback=False)
    # Use identical dates length across synthetic universe
    df_flat = _frame_from_closes([10.0 * (1.0005**i) for i in range(90)])
    csi = _frame_from_closes([4000.0] * 90)
    res = scan_slow_bull_candidates(
        {"600887": df_pullback, "600025": df_watch, "000002": df_flat},
        csi300_df=csi,
        chinext_df=csi,
        name_map={"600887": "伊利股份", "600025": "华能水电", "000002": "平稳对照"},
        market_cap_map={"600887": 1800.0, "600025": 1700.0, "000002": 400.0},
        sector_map={"600887": "食品饮料", "600025": "电力", "000002": "房地产"},
        financial_map={"600887": fin, "600025": fin, "000002": fin},
    )
    assert res.counts["input_symbols"] == 3
    assert res.counts["pit_eligible"] == 3
    assert res.counts["moat_passed"] == 3
    assert res.counts["pullback_count"] == 1
    assert res.counts["watch_count"] == 1
    assert np.isclose(res.friction_round_trip_pct, 0.202, atol=0.05)
