"""影子车道同动量对照的检验。

重点不是「函数跑通」，而是三件容易静默出错的事：
1. 样本不足时必须显式降级，不能造一个假判定；
2. 面板缺失时必须说「没出对照」，不能让读者以为「没这一节」等于「过了」；
3. 对照真的接上了——一个候选组明显跑赢/跑平同动量同侪时，verdict 要跟着变。
"""

from __future__ import annotations

import math
import random

import pandas as pd
import pytest

from core.funnel_effect_eval import MIN_DAYS
from core.funnel_effect_panels import build_panels
from workflows.review_shadow_backtest import ShadowTrade
from workflows.review_shadow_control import (
    control_verdict_lines,
    evaluate_lane_control,
    lane_control_summary,
    lane_day_map,
)

# 20 天动量预热 + 尾部 T+1+5 窗口都吃掉日子，要过 MIN_DAYS=20 得留够总天数。
DAYS = 60
POOL_SIZE = 40
# 日收益噪声标准差(%)。作用见 _market_frame 的说明：没有噪声，配对会把
# 「有边缘」那一侧也一并杀掉，fixture 就只能验证 null。
NOISE_PCT = 1.5


def _trade(signal_date: str, code: str, *, lane: str = "pre_breakout") -> ShadowTrade:
    return ShadowTrade(
        signal_date=signal_date,
        entry_date=signal_date,
        code=code,
        name=code,
        lane=lane,
        score=50.0,
        ranked=True,
        entry_open=10.0,
        signal_pct_chg=1.0,
        next_pct_chg=1.0,
        review_hit=False,
        open_executable=True,
        intraday_executable=True,
        ret_t1_pct=1.0,
        ret_t3_pct=1.0,
        ret_t5_pct=1.0,
        mfe_t5_pct=2.0,
        mae_t5_pct=-1.0,
    )


def _market_frame(*, hit_daily_pct: float, hit_codes: list[str]) -> pd.DataFrame:
    """构造 DAYS 天全市场行情：日收益带噪声，命中票另加一个固定日边缘。

    为什么必须带噪声：最近邻动量配对**天然会杀掉「纯粹由过去 20 日涨幅决定的」
    收益差**。若每只票都是恒定日涨幅，过去 20 日涨幅就是未来收益的完美代理，
    配好的对照与命中票收益必然相同、超额恒为 0——那样这个 fixture 只能验证 null
    的一侧，永远验不出「有边缘」的那一侧，也就无法证明这套对照有分辨力。

    噪声让过去动量与未来收益解耦：配对按过去动量找同侪，而命中票的
    ``hit_daily_pct`` 是独立于动量的增量，正是「选股信息」该有的形态。
    """
    dates = pd.bdate_range("2026-01-05", periods=DAYS).strftime("%Y-%m-%d")
    rows: list[dict[str, object]] = []
    for idx in range(POOL_SIZE):
        _append_series(rows, f"{idx:06d}", dates, 0.0)
    for code in hit_codes:
        _append_series(rows, code, dates, hit_daily_pct)
    return pd.DataFrame(rows).sort_values(["code", "ds"]).reset_index(drop=True)


def _append_series(rows: list[dict[str, object]], code: str, dates: pd.Index, edge_pct: float) -> None:
    """按 code 定种子，结果可复现；日收益 = 噪声 + 边缘。"""
    rng = random.Random(f"{code}-fixture")
    price = 10.0
    for ds in dates:
        daily = rng.gauss(0.0, NOISE_PCT) + edge_pct
        open_price = price * (1.0 + daily / 200.0)
        price *= 1.0 + daily / 100.0
        rows.append(
            {
                "code": code,
                "ds": ds,
                "open": round(open_price, 4),
                "close": round(price, 4),
                "amt_wan": 50000.0,
            }
        )


def _panels_and_trades(*, hit_daily_pct: float) -> tuple[object, list[ShadowTrade]]:
    hit_codes = [f"9{idx:05d}" for idx in range(5)]
    panels = build_panels(_market_frame(hit_daily_pct=hit_daily_pct, hit_codes=hit_codes))
    # 前 20 天没有 mom20（需要 shift(20)），信号日从第 21 天起，尾部留出 T+1+5 的窗口。
    signal_days = panels.dates[21:-7]
    trades = [_trade(ds, code) for ds in signal_days for code in hit_codes]
    return panels, trades


def test_lane_day_map_groups_by_signal_date() -> None:
    trades = [_trade("2026-01-05", "000001"), _trade("2026-01-05", "000002"), _trade("2026-01-06", "000003")]

    assert lane_day_map(trades) == {
        "2026-01-05": {"formal_l4": ["000001", "000002"], "all": ["000001", "000002"]},
        "2026-01-06": {"formal_l4": ["000003"], "all": ["000003"]},
    }


def test_lane_control_degrades_when_days_below_minimum() -> None:
    """4 天的老 fixture 不能出判定,只能说样本不足。"""
    panels, _ = _panels_and_trades(hit_daily_pct=0.0)
    trades = [_trade(ds, "900000") for ds in panels.dates[21:24]]

    result = evaluate_lane_control(trades, panels)

    assert result["eligible"] is False
    assert result["signal_days"] == 3
    assert str(MIN_DAYS) in result["reason"]


def test_lane_control_reports_unavailable_without_panels() -> None:
    """缺快照要显式说没出对照——不能让「没这一节」被读成「过了」。"""
    summary = lane_control_summary([_trade("2026-01-05", "000001")], None)

    assert summary["available"] is False
    assert "无法构建同动量对照" in summary["reason"]
    assert "未出对照" in "\n".join(control_verdict_lines(summary))


def test_lane_control_finds_no_edge_when_hits_match_peers() -> None:
    """命中票与同动量同侪同收益时,配对超额应≈0 且被随机负控制吃掉。"""
    panels, trades = _panels_and_trades(hit_daily_pct=0.0)

    result = evaluate_lane_control(trades, panels, horizons=(5,))
    cell = result["horizons"]["5"]

    assert result["eligible"] is True
    assert result["signal_days"] >= MIN_DAYS
    assert cell["matched"]["days"] >= MIN_DAYS
    assert abs(cell["matched"]["excess_pct"]) < 0.5
    assert "不含选股信息" in cell["control_gap"]["verdict"]


def test_lane_control_detects_edge_beyond_random_control() -> None:
    """命中票每天多赚 0.5% 时,配对超额要跑赢随机负控制——否则这套对照没有分辨力。"""
    panels, trades = _panels_and_trades(hit_daily_pct=0.5)

    cell = evaluate_lane_control(trades, panels, horizons=(5,))["horizons"]["5"]

    assert cell["matched"]["excess_pct"] > 1.0
    assert "含独立选股信息" in cell["control_gap"]["verdict"]
    # 配对后残差动量不该系统性偏向命中组,否则超额里混着动量 beta。
    assert abs(cell["matched"]["residual_mom_pct"]) < 3.0


def test_lane_control_reports_absolute_and_excess_together() -> None:
    """两栏分母不同必须同时出:超额为正不等于赚钱。"""
    panels, trades = _panels_and_trades(hit_daily_pct=0.5)

    cell = evaluate_lane_control(trades, panels, horizons=(5,))["horizons"]["5"]
    absolute, matched = cell["absolute"], cell["matched"]

    assert absolute["avg_size"] >= matched["avg_size"]
    assert absolute["net_pct"] is not None
    assert absolute["verdict"] != "样本不足"
    assert not math.isnan(float(matched["excess_t"]))


def test_control_verdict_lines_render_each_lane() -> None:
    panels, trades = _panels_and_trades(hit_daily_pct=0.5)
    summary = lane_control_summary(trades, panels, horizons=(5,))

    text = "\n".join(control_verdict_lines(summary))

    assert "## 同动量对照" in text
    assert "pre_breakout" in text
    assert "T+5" in text
    assert "绝对" in text and "配对超额" in text


def test_lane_control_multiple_lanes_are_evaluated_separately() -> None:
    panels, trades = _panels_and_trades(hit_daily_pct=0.5)
    tagged = [
        _trade(t.signal_date, t.code, lane="near_l2" if t.code.endswith(("0", "1")) else "pre_breakout") for t in trades
    ]

    lanes = lane_control_summary(tagged, panels, horizons=(5,))["lanes"]

    assert set(lanes) == {"near_l2", "pre_breakout"}
    for block in lanes.values():
        assert "signal_days" in block


@pytest.mark.parametrize("hit_daily_pct", [0.0, 0.5])
def test_lane_control_horizons_all_present(hit_daily_pct: float) -> None:
    panels, trades = _panels_and_trades(hit_daily_pct=hit_daily_pct)

    horizons = evaluate_lane_control(trades, panels)["horizons"]

    assert set(horizons) == {"1", "3", "5"}
