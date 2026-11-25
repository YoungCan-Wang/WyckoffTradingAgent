from __future__ import annotations

from datetime import date

import workflows.slow_bull_scan as slow_bull_scan
from integrations.fetch_a_share_csv import TradingWindow
from tests.core.test_slow_bull_engine import _frame_from_closes, _stage_b_fixture


def test_run_slow_bull_scan_targeted_diagnosis_is_offline_and_research_only(monkeypatch) -> None:
    df_600887 = _stage_b_fixture(last_volume=1_100_000.0, pullback=True)
    csi = _frame_from_closes([4000.0] * len(df_600887))
    fin = {"roe": 15.0, "period_end": "2026-09-30", "debt_to_asset_ratio": 45.0, "operating_cash_to_revenue": 14.0}

    monkeypatch.setattr(
        slow_bull_scan,
        "resolve_trading_window",
        lambda **_k: TradingWindow(date(2026, 1, 1), date(2026, 5, 6)),
    )
    monkeypatch.setattr(slow_bull_scan, "fetch_index_hist", lambda *_a, **_k: csi)
    monkeypatch.setattr(slow_bull_scan, "load_stock_name_map", lambda: {"600887": "伊利股份"})
    monkeypatch.setattr(slow_bull_scan, "fetch_sector_map", lambda: {"600887": "食品饮料"})
    monkeypatch.setattr(
        slow_bull_scan,
        "fetch_slow_bull_daily_basic_map",
        lambda **_k: {"600887": {"market_cap_yi": 1800.0, "dv_ttm_pct": 4.2, "turnover_pct": 1.1}},
    )
    monkeypatch.setattr(slow_bull_scan, "fetch_all_ohlcv", lambda *_a, **_k: ({"600887": df_600887}, {"fetch_ok": 1}))
    monkeypatch.setattr(slow_bull_scan, "_load_financial_map", lambda *_a, **_k: {"600887": fin})

    res = slow_bull_scan.run_slow_bull_scan(slow_bull_scan.SlowBullScanRequest(codes=("600887",)))

    assert res["ok"] is True
    assert res["job_kind"] == "slow_bull_scan"
    assert res["mode"] == "targeted_diagnosis"
    assert res["research_only"] is True
    assert res["new_buy_allowed"] is False
    assert res["counts"]["pullback_count"] == 1
    assert "slow_bull_pullback" in res["markdown_report"]
    assert "600887" in res["result_summary"]


def test_run_slow_bull_scan_market_mode(monkeypatch) -> None:
    df_pullback = _stage_b_fixture(last_volume=1_100_000.0, pullback=True)
    df_other = _frame_from_closes([10.0 * (1.0005**i) for i in range(len(df_pullback))])
    csi = _frame_from_closes([4000.0] * len(df_pullback))
    fin = {"roe": 12.0, "period_end": "2026-09-30", "debt_to_asset_ratio": 45.0, "operating_cash_to_revenue": 12.0}

    monkeypatch.setattr(
        slow_bull_scan,
        "resolve_trading_window",
        lambda **_k: TradingWindow(date(2026, 1, 1), date(2026, 5, 6)),
    )
    monkeypatch.setattr(slow_bull_scan, "fetch_index_hist", lambda *_a, **_k: csi)
    monkeypatch.setattr(slow_bull_scan, "load_stock_name_map", lambda: {"600887": "伊利股份", "600000": "浦发银行"})
    monkeypatch.setattr(slow_bull_scan, "fetch_sector_map", lambda: {"600887": "食品饮料", "600000": "银行"})
    monkeypatch.setattr(
        slow_bull_scan,
        "fetch_slow_bull_daily_basic_map",
        lambda **_k: {
            "600887": {"market_cap_yi": 1800.0, "dv_ttm_pct": 4.2, "turnover_pct": 1.1},
            "600000": {"market_cap_yi": 2500.0, "dv_ttm_pct": 4.0, "turnover_pct": 0.6},
        },
    )
    monkeypatch.setattr(
        slow_bull_scan,
        "resolve_symbol_pool",
        lambda **_k: (["600887", "600000"], {"600887": "伊利股份", "600000": "浦发银行"}, {}),
    )
    monkeypatch.setattr(
        slow_bull_scan,
        "fetch_all_ohlcv",
        lambda *_a, **_k: ({"600887": df_pullback, "600000": df_other}, {"fetch_ok": 2}),
    )
    monkeypatch.setattr(slow_bull_scan, "_load_financial_map", lambda *_a, **_k: {"600887": fin, "600000": fin})

    res = slow_bull_scan.run_slow_bull_scan(slow_bull_scan.SlowBullScanRequest(board="main_chinext_star", limit=10))

    assert res["ok"] is True
    assert res["mode"] == "market_scan"
    assert res["research_only"] is True
    assert res["new_buy_allowed"] is False
    assert res["counts"]["pullback_count"] == 1
