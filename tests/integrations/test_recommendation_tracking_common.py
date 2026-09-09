import math

import pandas as pd

from integrations.recommendation_tracking_common import ohlc_map_from_tickflow_hist


def test_event_bars_preserve_missing_prices_and_entry_fields() -> None:
    frame = pd.DataFrame(
        [
            {"date": "2026-09-07", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 0},
            {"date": "2026-09-08", "open": 10, "high": 11, "low": 9, "close": None, "volume": 1},
        ]
    )
    bars = ohlc_map_from_tickflow_hist(frame, preserve_invalid=True)
    assert bars["20260907"]["open"] == 10
    assert bars["20260907"]["volume"] == 0
    assert math.isnan(bars["20260908"]["close"])
    assert "20260908" not in ohlc_map_from_tickflow_hist(frame)
