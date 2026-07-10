from __future__ import annotations

from datetime import date

import pandas as pd

from core.wbt_adapter import build_position_weight_frame


def test_position_weight_frame_uses_next_day_entry_and_exit_zero_weight() -> None:
    trade_dates = [date(2026, 1, day) for day in range(1, 5)]
    frame = build_position_weight_frame(
        records=[{"code": "000001", "signal_date": date(2026, 1, 1), "exit_date": date(2026, 1, 3)}],
        all_df_map={
            "000001": pd.DataFrame(
                {
                    "date": trade_dates,
                    "close": [10.0, 11.0, 12.0, 13.0],
                }
            )
        },
        ohlc_cache={},
        trade_dates=trade_dates,
        start_dt=date(2026, 1, 1),
        end_dt=date(2026, 1, 4),
    )

    weights = {row.dt.date(): row.weight for row in frame.itertuples()}

    assert weights[date(2026, 1, 1)] == 0.0
    assert weights[date(2026, 1, 2)] == 1.0
    assert weights[date(2026, 1, 3)] == 0.0
    assert weights[date(2026, 1, 4)] == 0.0
    assert list(frame["price"]) == [10.0, 11.0, 12.0, 13.0]
