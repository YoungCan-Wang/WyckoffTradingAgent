from __future__ import annotations

import pandas as pd

from core.backtest_metrics import stats_for_trade_slice


def test_stats_for_trade_slice_keeps_stop_rate_none_without_exit_reason() -> None:
    stats = stats_for_trade_slice(pd.DataFrame([{"ret_pct": 1.0}, {"ret_pct": -2.0}, {"ret_pct": 3.0}]))

    assert stats["trades"] == 3
    assert stats["stop_exit_rate_pct"] is None
