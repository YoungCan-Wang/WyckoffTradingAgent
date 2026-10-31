from __future__ import annotations

import pandas as pd

from core.market_sentiment import SENTIMENT_COLUMNS, build_market_sentiment


def _frame(closes: list[float], highs: list[float] | None = None) -> pd.DataFrame:
    dates = pd.date_range("2021-01-04", periods=len(closes), freq="B").date
    highs = highs or closes
    return pd.DataFrame({"date": dates, "open": closes, "high": highs, "low": closes, "close": closes})


def test_counts_limits_streaks_and_broken_boards_by_board_rule() -> None:
    # 主板 10%：连续两个涨停后回落；第 4 日触板未封。
    main = _frame([10.0, 11.0, 12.1, 11.0, 11.5], highs=[10.0, 11.0, 12.1, 13.31, 11.5])
    # 创业板 2021 年按 20%：涨 10% 不算涨停，涨 20% 算。
    chinext = _frame([10.0, 11.0, 13.2, 13.2, 10.56])
    # ST 主板 5%：跌 5% 即跌停。
    st = _frame([10.0, 9.5, 9.5, 9.5, 9.5])
    out = build_market_sentiment({"600000": main, "300001": chinext, "600001": st}, {"600001": "ST测试"})

    assert list(out.columns) == list(SENTIMENT_COLUMNS)
    assert len(out) == 4  # 首日无前收，不计
    day2, day3, day4, day5 = (out.iloc[i] for i in range(4))
    assert (day2["up_limit"], day2["down_limit"], day2["max_streak"]) == (1, 1, 1)
    assert (day3["up_limit"], day3["max_streak"]) == (2, 2)
    assert (day4["up_limit"], day4["touched_up"], day4["broke_up"]) == (0, 1, 1)
    assert (day5["up_limit"], day5["down_limit"], day5["decliners"]) == (0, 1, 1)
    assert out["symbols"].tolist() == [3, 3, 3, 3]
