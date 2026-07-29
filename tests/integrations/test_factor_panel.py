"""交易日历及 PIT ST 标记的边界。"""

from __future__ import annotations

import pandas as pd
from integrations.factor_panel import canonicalize_name_intervals, st_flags, trade_dates


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


def test_canonicalize_prefers_closed_interval_over_open_twin() -> None:
    names = pd.DataFrame(
        [
            {"ts_code": "002122.SZ", "name": "ST天马", "start_date": "20230110", "end_date": None},
            {"ts_code": "002122.SZ", "name": "ST天马", "start_date": "20230110", "end_date": "20230227"},
            {"ts_code": "002122.SZ", "name": "天马科技", "start_date": "20230228", "end_date": None},
        ]
    )
    out = canonicalize_name_intervals(names)
    st_rows = out[out["name"] == "ST天马"]
    assert len(st_rows) == 1
    assert st_rows.iloc[0]["end_date"] == "20230227"


def test_st_flags_stop_after_rehab_when_open_and_closed_twins_exist() -> None:
    """开闭区间双胞胎不得把摘帽后的交易日继续标成 ST。"""
    names = pd.DataFrame(
        [
            {"ts_code": "002122.SZ", "name": "ST天马", "start_date": "20230110", "end_date": None},
            {"ts_code": "002122.SZ", "name": "ST天马", "start_date": "20230110", "end_date": "20230227"},
            {"ts_code": "002122.SZ", "name": "天马科技", "start_date": "20230228", "end_date": "99999999"},
        ]
    )
    dates = pd.to_datetime(["2023-02-20", "2023-02-28", "2023-03-15"])
    flags = st_flags(names, dates).set_index("date")["is_st"]
    assert bool(flags.get(pd.Timestamp("2023-02-20"), False)) is True
    assert bool(flags.get(pd.Timestamp("2023-02-28"), False)) is False
    assert bool(flags.get(pd.Timestamp("2023-03-15"), False)) is False
