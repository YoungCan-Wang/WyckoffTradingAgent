from __future__ import annotations

import math
from dataclasses import dataclass

from workflows.backtest_market_report_artifacts import win_rate_span


@dataclass(frozen=True)
class _Cell:
    """只带 win_rate 的替身:win_rate_span 只读这一个字段,不必造完整 GridCell。"""

    win_rate: float | None


def test_win_rate_span_returns_average_and_worst():
    avg, worst = win_rate_span([_Cell(60.0), _Cell(40.0), _Cell(30.0)])

    assert worst == 30.0
    assert avg == (60.0 + 40.0 + 30.0) / 3


def test_win_rate_span_keeps_zero_as_a_real_reading():
    """胜率 0.0 是「一笔没赢」这个真实取值,不能跟缺值一样被丢掉。

    丢了它,最差周期会读成剩下那些里最小的那个,把一段完全没赢的周期抹成看着还行。
    """
    avg, worst = win_rate_span([_Cell(0.0), _Cell(50.0)])

    assert worst == 0.0
    assert avg == 25.0


def test_win_rate_span_skips_missing_values():
    avg, worst = win_rate_span([_Cell(None), _Cell(40.0), _Cell(None), _Cell(20.0)])

    assert worst == 20.0
    assert avg == 30.0


def test_win_rate_span_drops_nan_regardless_of_position():
    """NaN 必须按位置无关地剔除。

    ``min()`` 碰到 NaN 的结果取决于它在序列里的位置:``min([1.0, nan])`` 得 1.0、
    ``min([nan, 1.0])`` 得 nan。所以只测一种摆法测不出来——NaN 放第一位时,没过滤的实现
    会静默把整组的最差周期改写成 NaN,而报表那一格只会显示成 ``-``,看着像"没这个数"。
    """
    assert math.isnan(min([float("nan"), 1.0])), "构造无效：min 已不再受 NaN 位置影响"

    leading = win_rate_span([_Cell(float("nan")), _Cell(40.0), _Cell(20.0)])
    trailing = win_rate_span([_Cell(40.0), _Cell(20.0), _Cell(float("nan"))])

    assert leading == trailing == (30.0, 20.0)


def test_win_rate_span_returns_none_when_nothing_is_measurable():
    """全缺值时给 (None, None),让 ``_fmt_num`` 显示 ``-``,而不是 0.0 冒充胜率为零。"""
    assert win_rate_span([_Cell(None), _Cell(float("inf"))]) == (None, None)
    assert win_rate_span([]) == (None, None)
