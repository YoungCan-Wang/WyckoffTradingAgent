"""Tests for the momentum checkup's random negative control sampler.

这个对照组是第二轮唯一能否掉「退到中动量档」的环节：中动量档超额 +0.197（t=5.86）
看着很硬，但每天从「两条腿都低于 NON_TOP_CAP 分位」随机抽同样只数，5 个种子给出
+0.164~+0.210——无法区分。它的全部边缘来自避开顶部，不含选择信息。

所以采样器有三件事必须守住，否则对照会失效或掺进噪声：

1. **只砍顶部**，域的其余部分不能动，否则比较的就不是同一件事。
2. **只数与中动量档当日相同**，只数不同会把规模效应读成选择价值。
3. **同一天同一种子必须取到同一批票**，跨 horizon、跨运行都一致，否则各档之间的
   差值里会掺进抽样噪声。
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.momentum_regime_eval import CONTROL_SEEDS, NON_TOP_CAP, PROD_RPS_WINDOW_SLOW
from scripts.evaluate_momentum_regime import (
    _collect_non_top_control,
    amount_to_raw_divisor,
    eval_window,
)


def _cross_section(n: int = 200):
    """构造截面：两条腿的分位都等于 0..100 的均匀刻度，前向收益等于分位本身。

    forward 与分位同增，因此「避开顶部」必然降低组内均值——这让「域被正确裁剪」
    这件事可以直接从数值上读出来。
    """
    codes = [f"{i:06d}.SZ" for i in range(n)]
    pct = pd.Series([i * 100.0 / (n - 1) for i in range(n)], index=codes)
    return pct, pct.copy(), pct.copy()


def _run(size: int = 30, date: int = 20260415, domain_ret: float = 0.5, n: int = 200):
    pct_fast, pct_slow, forward = _cross_section(n)
    sink: dict[str, list[dict[str, float]]] = {}
    _collect_non_top_control(pct_fast, pct_slow, forward, date, size, domain_ret, sink)
    return sink


class TestControlSampler:
    def test_one_row_per_seed(self):
        sink = _run()
        assert sorted(sink) == sorted(f"__control_{seed}__" for seed in CONTROL_SEEDS)
        assert all(len(rows) == 1 for rows in sink.values())

    def test_size_matches_the_mid_band(self):
        """只数不同会把规模效应读成选择价值。"""
        sink = _run(size=37)
        assert all(rows[0]["size"] == pytest.approx(37.0) for rows in sink.values())

    def test_only_the_top_is_removed(self):
        """抽样域是「两条腿都 < NON_TOP_CAP 分位」，其余部分不能动。

        forward 与分位同增，所以组内均值必须落在裁剪后域的均值附近、明显低于全域。
        """
        pct_fast, pct_slow, forward = _cross_section(n=400)
        eligible = forward[(pct_fast < NON_TOP_CAP) & (pct_slow < NON_TOP_CAP)]
        sink = _run(size=120, n=400)
        insides = [rows[0]["inside"] for rows in sink.values()]
        assert max(insides) < float(forward.mean())
        # 每个种子都是同一域内的无偏抽样，均值应在域均值附近而非贴着上下界。
        assert all(abs(v - float(eligible.mean())) < 8.0 for v in insides)
        assert max(insides) <= float(eligible.max())

    def test_domain_ret_is_passed_through_untouched(self):
        """对照必须与其它档共用同一个域内基准，否则 excess 不可比。"""
        sink = _run(domain_ret=-1.25)
        assert all(rows[0]["domain"] == pytest.approx(-1.25) for rows in sink.values())

    def test_same_seed_and_date_is_reproducible(self):
        """跨运行、跨 horizon 取到同一批票，否则各档差值里会掺进抽样噪声。"""
        first = _run()
        second = _run()
        for key, rows in first.items():
            assert rows[0]["inside"] == pytest.approx(second[key][0]["inside"])

    def test_seeds_differ_from_each_other(self):
        """多种子的意义是量出抽样边缘的宽度，全都相同就白搭了。"""
        insides = {round(rows[0]["inside"], 6) for rows in _run().values()}
        assert len(insides) == len(CONTROL_SEEDS)

    def test_dates_differ_from_each_other(self):
        """同一种子在不同交易日必须换一批票，否则对照会锁死在一次抽样上。"""
        one = _run(date=20260415)
        two = _run(date=20260416)
        assert any(one[k][0]["inside"] != pytest.approx(two[k][0]["inside"]) for k in one)

    def test_too_few_eligible_names_records_nothing(self):
        """裁剪后不够抽满时整天跳过，不能悄悄抽少了充数。"""
        sink = _run(size=500)
        assert sink == {}


class TestEvalWindow:
    """``--start`` 不是评估区间：预热把起点往后推 120 个交易日。

    实测同一份行情，start=2025-01 只剩 271 天可评估（2025-07-04 起），生产档超额读
    +0.070（t=+0.28）「期望为零」；start=2024-01 有 513 天，同一个档位是 -0.320
    （t=-2.08）「负贡献」。区间必须显式落盘，否则会把没评估过的半年当成已覆盖。
    """

    def test_start_is_pushed_back_by_the_warmup(self):
        dates = list(range(20240101, 20240101 + 400))
        row = eval_window(dates, 10)[0]
        assert row["market_start"] == dates[0]
        assert row["eval_start"] == dates[PROD_RPS_WINDOW_SLOW]
        assert row["market_days"] == 400.0
        # 尾部还要留 H+1 天出前向窗口。
        assert row["eval_days"] == 400.0 - PROD_RPS_WINDOW_SLOW - 11.0

    def test_longer_horizon_costs_tail_days(self):
        dates = list(range(20240101, 20240101 + 400))
        assert eval_window(dates, 20)[0]["eval_days"] == eval_window(dates, 10)[0]["eval_days"] - 10.0

    def test_eval_end_is_the_last_evaluated_day(self):
        dates = list(range(20240101, 20240101 + 200))
        row = eval_window(dates, 5)[0]
        # 循环上界是 len-h-1（不含），故最后一个被评估的是它减一。
        assert row["eval_end"] == dates[200 - 5 - 1 - 1]

    def test_window_shorter_than_the_warmup_is_empty(self):
        assert eval_window(list(range(20240101, 20240101 + PROD_RPS_WINDOW_SLOW)), 10) == []


class TestAmountUnit:
    """两种行情源的 amount 差 1000 倍，不归一就等于换掉整个流动性域且不报错。

    tushare ``amount`` 单位千元，``amount/(vol*close)`` 中位数实测 0.0996；
    backtest 快照 hist_full 单位是元，同一比值 100.86。``MIN_AMOUNT_RAW`` 按前者定。
    """

    def _frame(self, scale: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "close": [10.0] * 50,
                "vol": [1000.0] * 50,
                "amount": [10.0 * 1000.0 * scale] * 50,
            }
        )

    def test_tushare_thousand_yuan_is_left_alone(self):
        assert amount_to_raw_divisor(self._frame(0.1)) == 1.0

    def test_snapshot_yuan_is_divided_by_a_thousand(self):
        assert amount_to_raw_divisor(self._frame(100.0)) == 1000.0

    def test_zero_and_missing_rows_do_not_decide_the_unit(self):
        frame = self._frame(0.1)
        frame.loc[0:9, "vol"] = 0.0
        frame.loc[10:19, "amount"] = float("nan")
        assert amount_to_raw_divisor(frame) == 1.0

    def test_unusable_frame_falls_back_to_no_scaling(self):
        frame = self._frame(0.1)
        frame["vol"] = 0.0
        assert amount_to_raw_divisor(frame) == 1.0
