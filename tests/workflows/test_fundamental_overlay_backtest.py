from pathlib import Path

import pandas as pd

from workflows.fundamental_overlay_backtest import (
    attach_point_in_time_overlay,
    build_overlay_evidence,
    latest_public_record,
    load_trade_files,
)


def test_latest_public_record_excludes_same_day_announcement() -> None:
    records = [
        {"announce_date": "2025-04-01", "period_end": "2024-12-31", "roe": 10},
        {"announce_date": "2025-04-02", "period_end": "2025-03-31", "roe": 20},
    ]

    selected = latest_public_record(records, "2025-04-02")

    assert selected["roe"] == 10


def test_trade_loader_and_overlay_evidence(tmp_path: Path) -> None:
    trade_dir = tmp_path / "window-a" / "backtest-grid-h5-sl0-tp0-tr0"
    trade_dir.mkdir(parents=True)
    path = trade_dir / "trades_20250101_20250331_h5_n0.csv"
    pd.DataFrame(
        [
            {"signal_date": "2025-03-01", "code": "1", "ret_pct": -12},
            {"signal_date": "2025-03-02", "code": "2", "ret_pct": 5},
        ]
    ).to_csv(path, index=False)
    history = {
        "000001": [
            {
                "announce_date": "2025-02-01",
                "period_end": "2024-12-31",
                "roe": -2,
                "net_income_yoy": -40,
                "revenue_yoy": -25,
                "operating_cash_to_revenue": -1,
            }
        ],
        "000002": [
            {
                "announce_date": "2025-02-01",
                "period_end": "2024-12-31",
                "roe": 20,
                "net_income_yoy": 10,
                "revenue_yoy": 10,
                "gross_margin": 35,
            }
        ],
    }

    trades = load_trade_files([path])
    enriched = attach_point_in_time_overlay(trades, history)
    evidence = build_overlay_evidence(enriched)

    assert enriched["fundamental_grade"].tolist() == ["weak", "strong"]
    assert enriched["research_window"].unique().tolist() == ["window-a"]
    assert evidence["overall"]["overlay"]["trades"] == 1
    assert evidence["overall"]["delta"]["avg_ret_pct"] == 8.5
    assert evidence["horizons"]["5"]["positive_window_ratio"] == 1.0
    assert evidence["grade_cohorts"]["weak"]["trades"] == 1
