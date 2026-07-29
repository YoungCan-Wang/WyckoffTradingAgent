"""交易日历取数的边界。"""

from __future__ import annotations

import pandas as pd

from integrations.factor_panel import trade_dates


class _Pro:
    """记录 trade_cal 实际收到的参数；只在无横线格式下返回数据，模仿 tushare 的行为。"""

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def trade_cal(self, **kwargs) -> pd.DataFrame:
        self.seen = kwargs
        if "-" in kwargs["start_date"] or "-" in kwargs["end_date"]:
            return pd.DataFrame()
        return pd.DataFrame({"cal_date": ["20260102", "20260105"]})


def test_dashed_dates_are_normalised_before_the_calendar_call():
    pro = _Pro()

    assert trade_dates(pro, "2026-01-01", "2026-01-05") == ["20260102", "20260105"]
    assert pro.seen["start_date"] == "20260101"
    assert pro.seen["end_date"] == "20260105"
