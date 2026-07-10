from __future__ import annotations

import pandas as pd

from workflows.step2_signal_confirmation import build_pending_signal_rows


def test_build_pending_signal_rows_keeps_snap_and_metadata():
    frame = pd.DataFrame(
        {
            "date": ["2026-05-18", "2026-05-19"],
            "open": [9.9, 10.2],
            "high": [10.2, 11.0],
            "low": [9.8, 10.1],
            "close": [10.0, 10.8],
            "volume": [1000, 1800],
        }
    )

    rows = build_pending_signal_rows(
        signal_date="2026-05-19",
        triggers={"sos": [("000001", 2.5)], "spring": [("000002", 1.0)]},
        df_map={"000001": frame},
        regime="RISK_ON",
        name_map={"000001": "平安银行"},
        sector_map={"000001": "银行"},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["code"] == 1
    assert row["signal_type"] == "sos"
    assert row["ttl_days"] == 2
    assert row["regime"] == "RISK_ON"
    assert row["name"] == "平安银行"
    assert row["industry"] == "银行"
    assert row["snap_close"] == 10.8


def test_build_pending_signal_rows_writes_candidate_metadata():
    frame = pd.DataFrame(
        {
            "date": ["2026-06-24", "2026-06-25"],
            "open": [20.0, 20.5],
            "high": [20.8, 21.2],
            "low": [19.8, 20.2],
            "close": [20.4, 21.0],
            "volume": [1000, 1200],
        }
    )

    rows = build_pending_signal_rows(
        signal_date="2026-06-25",
        triggers={"mainline": [("300308", 88.0)]},
        df_map={"300308": frame},
        candidate_metadata_map={
            "300308": {
                "strategy_version": "candidate_lane_v1",
                "candidate_lane": "mainline",
                "entry_type": "主线回踩MA20",
                "signal_key": "mainline",
                "mainline_score": 0.82,
                "timing_score": 0.7,
            }
        },
    )

    assert rows[0]["strategy_version"] == "candidate_lane_v1"
    assert rows[0]["candidate_lane"] == "mainline"
    assert rows[0]["entry_type"] == "主线回踩MA20"
    assert rows[0]["mainline_score"] == 0.82
    assert rows[0]["snap_support"] > 0
