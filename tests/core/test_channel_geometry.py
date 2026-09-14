"""Tests for the regression price channel (issue #429).

来源是雪球作者「趋势前沿」的两句纲领：趋势由价格通道指示、目标由黄金分割线逐线指示。
这里锁三件事——**无前视**、**lag 让「突破上轨」这个状态可表达**、**NaN 不被伪造**。

第二条最容易写错：lag=0 时上下轨取的是含当日残差的极值，价格被构造性地锁在通道内，
pos 恒为 0~100，而 #429 想否决的正是「破轨」类状态，届时因子恒为无信息量。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.channel_geometry import DEFAULT_LAG, DEFAULT_WINDOW, regression_channel_panels


def _frame(series: dict[str, list[float]], rows: int) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=rows)
    return pd.DataFrame(series, index=idx)


def _linear(rows: int, slope: float = 0.1, base: float = 10.0) -> list[float]:
    return [base + slope * i for i in range(rows)]


def _wiggly(rows: int, slope: float = 0.05, amp: float = 0.4, base: float = 10.0) -> list[float]:
    """带真实宽度的趋势通道。纯直线的通道宽度是浮点噪声，撑不起 pos 的定义域。"""
    return [base + slope * i + (amp if i % 2 else -amp) for i in range(rows)]


class TestNoLookahead:
    def test_truncating_the_future_does_not_change_today(self):
        """**最关键的一条**：T 日的值只能由 T 日及之前的数据决定。

        因子面板一旦偷看未来，IC 会虚高而实盘无法复现，且这类 bug 在全样本回测里
        完全不报错。用「砍掉尾部后重算」对比是唯一能抓住它的检查。
        """
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 40
        rng = np.random.default_rng(20260914)
        close = _frame({"600000": list(10 + np.cumsum(rng.normal(0, 0.2, rows)))}, rows)
        cut = rows - 10

        full = regression_channel_panels(close)
        truncated = regression_channel_panels(close.iloc[:cut])

        for name in ("slope", "pos", "width", "r2", "touches", "fib_room"):
            left = getattr(full, name).iloc[:cut]
            right = getattr(truncated, name)
            pd.testing.assert_frame_equal(left, right, check_exact=False, atol=1e-9)


class TestFit:
    def test_slope_matches_polyfit_on_a_straight_line(self):
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 5
        close = _frame({"600000": _linear(rows, slope=0.25)}, rows)

        panels = regression_channel_panels(close)

        # 斜率以「占价格的百分比」表达：0.25 元/bar 在收盘价上的占比。
        last = panels.slope["600000"].iloc[-1]
        assert last == pytest.approx(0.25 / close["600000"].iloc[-1] * 100.0, rel=1e-9)

    def test_perfect_line_has_r2_one_and_zero_width(self):
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 5
        close = _frame({"600000": _linear(rows)}, rows)

        panels = regression_channel_panels(close)

        assert panels.r2["600000"].iloc[-1] == pytest.approx(1.0, abs=1e-9)
        assert panels.width["600000"].iloc[-1] == pytest.approx(0.0, abs=1e-9)

    def test_warmup_rows_are_nan(self):
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 3
        close = _frame({"600000": _wiggly(rows)}, rows)

        panels = regression_channel_panels(close)

        head = panels.pos["600000"].iloc[: DEFAULT_WINDOW + DEFAULT_LAG - 1]
        assert head.isna().all()
        assert panels.pos["600000"].iloc[DEFAULT_WINDOW + DEFAULT_LAG - 1 :].notna().all()


class TestBreakoutIsExpressible:
    def test_pos_exceeds_one_hundred_after_a_gap_up(self):
        """lag>0 的全部意义：站到上轨之上要能被读出来。

        lag=0 时当日 close 参与残差极值，pos 数学上不可能越界，#429 的
        `破上轨` / `破下轨` 两个状态在因子里就不存在。
        """
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 1
        values = _wiggly(rows)
        values[-1] = values[-1] * 1.30  # 尾部一根 30% 跳空
        close = _frame({"600000": values}, rows)

        panels = regression_channel_panels(close)

        assert panels.pos["600000"].iloc[-1] > 100.0

    def test_lag_zero_locks_price_inside_the_channel(self):
        rows = DEFAULT_WINDOW + 1
        values = _wiggly(rows)
        values[-1] = values[-1] * 1.30
        close = _frame({"600000": values}, rows)

        panels = regression_channel_panels(close, lag=0)

        assert panels.pos["600000"].iloc[-1] == pytest.approx(100.0, abs=1e-9)

    def test_pos_goes_below_zero_after_a_gap_down(self):
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 1
        values = _wiggly(rows)
        values[-1] = values[-1] * 0.70
        close = _frame({"600000": values}, rows)

        panels = regression_channel_panels(close)

        assert panels.pos["600000"].iloc[-1] < 0.0


class TestMissingData:
    def test_short_history_column_stays_nan_without_touching_neighbours(self):
        """上市不足窗口的票不能被插补出一条轨——补出来的通道是假的，且不能污染同批的票。"""
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 5
        wiggly = [10.0 + 0.05 * i + (0.4 if i % 2 else -0.4) for i in range(rows)]
        short = [float("nan")] * (rows - 20) + _linear(20)
        close = _frame({"600000": wiggly, "301999": short}, rows)

        panels = regression_channel_panels(close)

        assert panels.pos["301999"].isna().all()
        assert np.isfinite(panels.pos["600000"].iloc[-1])

    def test_flat_series_has_no_channel_position(self):
        """全窗口同价（长期停牌复牌前）时通道宽度为 0，pos 无定义而非 0 或 100。"""
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 2
        close = _frame({"600000": [10.0] * rows}, rows)

        panels = regression_channel_panels(close)

        assert panels.pos["600000"].isna().iloc[-1]

    def test_perfect_line_is_not_a_channel(self):
        """只判 span>0 会被浮点噪声骗过：直线残差是 1e-14 级，span 为正但 pos 被放大成垃圾。

        一字板与极低波动窗口走的是同一条路径，所以宽度必须有相对下限（MIN_SPAN_PCT）。
        """
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 2
        close = _frame({"600000": _linear(rows, slope=0.1)}, rows)

        panels = regression_channel_panels(close)

        assert panels.pos["600000"].isna().iloc[-1]
        assert panels.touches["600000"].isna().iloc[-1]
        # 斜率与拟合度仍然有效——直线的趋势是真的，只有「在通道里的位置」无定义。
        assert np.isfinite(panels.slope["600000"].iloc[-1])
        assert panels.r2["600000"].iloc[-1] == pytest.approx(1.0, abs=1e-9)


class TestFibRoom:
    def _room_at(self, rows: int, close_now: float) -> float:
        values = _wiggly(rows)
        values[-1] = close_now
        return float(regression_channel_panels(_frame({"600000": values}, rows)).fib_room["600000"].iloc[-1])

    def test_room_is_a_sawtooth_not_a_monotone_distance(self):
        """同一档内价格每上行一步、到线距离就缩一步；越过一档则跳向上一条线。

        锯齿是定义使然，不是 bug——作者的用法本就是「逐线指示」，过一线看下一线。
        写死单调收窄的断言会把正确行为判成错误（拟合块不含当日，线位不随当日价格移动）。
        用扫描而非硬算线位：线位取决于拟合块的取窗方式，硬算会跟实现悄悄脱钩。
        """
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 1
        step = 0.1
        closes = [11.0 + step * i for i in range(59)]
        gaps = [c * self._room_at(rows, c) / 100.0 for c in closes]
        deltas = [b - a for a, b in zip(gaps, gaps[1:])]

        shrink = [d for d in deltas if d == pytest.approx(-step, abs=1e-9)]
        jumps = [d for d in deltas if d > 0]
        # 5 条分割线 + 1 个扩展位,最多 6 次跳档;其余每一步都必须精确缩 step。
        assert len(shrink) >= len(deltas) - 6
        assert jumps
        # 距离永远为正:线在价格下方时应换下一条,不该给出负的「还剩多少空间」。
        assert min(gaps) > 0

    def test_extension_level_used_once_price_clears_the_window_high(self):
        """越过窗口高点后 1.0 线已在身后，若不给 1.618 扩展位，目标距离就无定义。"""
        rows = DEFAULT_WINDOW + DEFAULT_LAG + 1
        values = _linear(rows, slope=0.05)
        values[-1] = max(values[:-1]) * 1.05
        close = _frame({"600000": values}, rows)

        room = regression_channel_panels(close).fib_room["600000"].iloc[-1]

        assert np.isfinite(room)
        assert room > 0.0
