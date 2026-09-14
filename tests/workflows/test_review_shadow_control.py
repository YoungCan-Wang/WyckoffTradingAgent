"""影子车道同动量对照的检验。

重点不是「函数跑通」，而是三件容易静默出错的事：
1. 样本不足时必须显式降级，不能造一个假判定；
2. 面板缺失时必须说「没出对照」，不能让读者以为「没这一节」等于「过了」；
3. 对照真的接上了——一个候选组明显跑赢/跑平同动量同侪时，verdict 要跟着变。

反向臂（``score_direction``）那半的重点另有两条：

4. 两半必须共用同一个非车道对照池。``all`` 若只填自己那半，另一半就落进对方的随机
   篮，两端超额被同一批票同时压低和抬高，差值读出来的不再是排序键的方向 —— 而报告
   照样印出一个像样的 t 值，是个长得跟正常证据一样的假结果。
5. 「符号反了」与「无信息」必须真的分开。两者在裸收益和整条车道的配对超额上完全同形，
   ``_direction_*`` fixture 把同一批票、同一批边缘按两种分值摆法各跑一次：只有反向臂
   读得出差别，才说明这条判据有分辨力。
"""

from __future__ import annotations

import math
import random

import pandas as pd
import pytest

from core.funnel_effect_eval import MIN_DAYS, MIN_HITS_PER_DAY
from core.funnel_effect_panels import build_panels
from workflows.review_shadow_backtest import ShadowTrade
from workflows.review_shadow_control import (
    DIRECTION_T_GATE,
    _direction_verdict,
    _excess_by_day,
    control_verdict_lines,
    direction_verdict_lines,
    evaluate_lane_control,
    evaluate_score_direction,
    lane_control_summary,
    lane_day_map,
    score_direction_summary,
    score_split_day_maps,
)

# 20 天动量预热 + 尾部 T+1+5 窗口都吃掉日子，要过 MIN_DAYS=20 得留够总天数。
DAYS = 60
POOL_SIZE = 40
# 日收益噪声标准差(%)。作用见 _market_frame 的说明：没有噪声，配对会把
# 「有边缘」那一侧也一并杀掉，fixture 就只能验证 null。
NOISE_PCT = 1.5
# 反向臂每日 8 只：每半 4 只 > MIN_HITS_PER_DAY=3，切得出两半又留一只的余量给配对失败。
DIRECTION_CODES_PER_DAY = 8
# 有边缘那半的日增量(%)。与对照那组同量级，T+5 约 2.5pct，远超随机负控制的离散度。
DIRECTION_EDGE_PCT = 0.5


def _trade(
    signal_date: str,
    code: str,
    *,
    lane: str = "pre_breakout",
    score: float | None = 50.0,
    ranked: bool = True,
) -> ShadowTrade:
    return ShadowTrade(
        signal_date=signal_date,
        entry_date=signal_date,
        code=code,
        name=code,
        lane=lane,
        score=score,
        ranked=ranked,
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


def _direction_frame(edges: dict[str, float]) -> pd.DataFrame:
    """逐码指定日边缘。反向臂要让边缘只落在一半票上，``_market_frame`` 的单一 edge 不够用。"""
    dates = pd.bdate_range("2026-01-05", periods=DAYS).strftime("%Y-%m-%d")
    rows: list[dict[str, object]] = []
    for idx in range(POOL_SIZE):
        _append_series(rows, f"{idx:06d}", dates, 0.0)
    for code, edge in edges.items():
        _append_series(rows, code, dates, edge)
    return pd.DataFrame(rows).sort_values(["code", "ds"]).reset_index(drop=True)


def _direction_panels_and_trades(*, edge_on_high: bool) -> tuple[object, list[ShadowTrade]]:
    """同一批票、同一批边缘，只改分值与边缘的对应关系。

    ``edge_on_high=True`` 把边缘给高分半（排序键方向正确），``False`` 给低分半（符号反了）。
    两种摆法用的是同一批票、同一批边缘，只换了分值与边缘的对应关系，所以裸收益完全相同、
    整条车道的配对超额也给出同一个 verdict（实测 +1.00pct 与 +0.76pct，两者都判「含独立选股
    信息」；数字不逐位相等是因为配对挑的近邻不同，但结论同形）。分开它们只能靠反向臂：
    同一批数据切两半后读出 +3.12pct 与 -2.88pct。这就是这个 fixture 存在的理由。
    """
    codes = [f"9{idx:05d}" for idx in range(DIRECTION_CODES_PER_DAY)]
    half = DIRECTION_CODES_PER_DAY // 2
    edged = set(codes[:half] if edge_on_high else codes[half:])
    panels = build_panels(_direction_frame({c: (DIRECTION_EDGE_PCT if c in edged else 0.0) for c in codes}))
    # 分值按 codes 顺序递减，故 codes[:half] 恒为高分半，与 edge_on_high 的语义对齐。
    scores = {code: float(100 - 5 * idx) for idx, code in enumerate(codes)}
    signal_days = panels.dates[21:-7]
    trades = [_trade(ds, code, score=scores[code]) for ds in signal_days for code in codes]
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


def test_score_split_shares_one_control_pool() -> None:
    """两半的 ``all`` 都得是整条车道 —— 这是整条判据的支点。

    ``resolve_layer`` 取 ``pool = universe - all``。``all`` 只填自己那半的话，另一半就
    进了对方的对照池，差值不再是排序键的方向。
    """
    trades = [_trade("2026-01-05", f"00000{idx}", score=float(idx)) for idx in range(6)]

    high, low, days = score_split_day_maps(trades)

    lane = sorted(t.code for t in trades)
    assert days == 1
    assert high["2026-01-05"]["all"] == lane
    assert low["2026-01-05"]["all"] == lane
    assert high["2026-01-05"]["formal_l4"] == ["000003", "000004", "000005"]
    assert low["2026-01-05"]["formal_l4"] == ["000000", "000001", "000002"]


def test_score_split_excludes_the_median_on_odd_counts() -> None:
    """奇数只时中位那只两半都不进：它落哪边纯看并列打破方式，且两半只数须相等。"""
    trades = [_trade("2026-01-05", f"00000{idx}", score=float(idx)) for idx in range(7)]

    high, low, _ = score_split_day_maps(trades)

    assert high["2026-01-05"]["formal_l4"] == ["000004", "000005", "000006"]
    assert low["2026-01-05"]["formal_l4"] == ["000000", "000001", "000002"]
    assert "000003" not in high["2026-01-05"]["formal_l4"] + low["2026-01-05"]["formal_l4"]


def test_score_split_drops_days_that_cannot_fill_both_halves() -> None:
    """每半都要够 MIN_HITS_PER_DAY，否则这一天不切 —— 不能一半 3 只一半 1 只硬凑。"""
    thin = [_trade("2026-01-05", f"00000{idx}", score=float(idx)) for idx in range(MIN_HITS_PER_DAY * 2 - 1)]

    high, low, days = score_split_day_maps(thin)

    assert (high, low, days) == ({}, {}, 0)


def test_score_split_skips_unranked_and_scoreless() -> None:
    """没有排序分就没有「高半 / 低半」；混进来会把未排序的票当成最低分。"""
    trades = [
        *(_trade("2026-01-05", f"00000{idx}", score=float(idx)) for idx in range(6)),
        _trade("2026-01-05", "888888", score=None),
        _trade("2026-01-05", "777777", score=1.0, ranked=False),
    ]

    high, low, _ = score_split_day_maps(trades)

    joined = high["2026-01-05"]["all"]
    assert "888888" not in joined and "777777" not in joined
    assert len(joined) == 6


def test_score_split_dedupes_same_code_within_a_day() -> None:
    """同日重复的票会同时进两半，把差值两头一起带偏。"""
    trades = [
        *(_trade("2026-01-05", f"00000{idx}", score=float(idx)) for idx in range(6)),
        _trade("2026-01-05", "000000", score=99.0),
    ]

    high, _, _ = score_split_day_maps(trades)

    assert len(high["2026-01-05"]["all"]) == 6
    # 保留首次出现那条(score=0.0)，故 000000 仍属低分半。
    assert "000000" not in high["2026-01-05"]["formal_l4"]


def test_excess_by_day_drops_rows_missing_either_column() -> None:
    """缺任一栏就丢掉那天，不按 0 补 —— 补 0 等于凭空造一个「恰好打平」的差值。"""
    rows = [
        {"date": "2026-01-05", "net": 2.0, "control": 0.5},
        {"date": "2026-01-06", "net": 2.0, "control": None},
        {"date": "2026-01-07", "net": None, "control": 0.5},
    ]

    assert _excess_by_day(rows) == {"2026-01-05": 1.5}


def test_score_direction_reports_unavailable_without_panels() -> None:
    """缺快照时这一节缺席，必须印出原因，不能被读成「方向没问题」。"""
    summary = score_direction_summary([_trade("2026-01-05", "000001")], None)

    assert summary["available"] is False
    assert "反向臂无从算起" in summary["reason"]
    assert "未出反向臂" in "\n".join(direction_verdict_lines(summary))


def test_score_direction_degrades_when_split_days_below_minimum() -> None:
    """样本不够是「下个月能重跑」，所以 ranked 仍为 True —— 与下一条配成一对。"""
    panels, trades = _direction_panels_and_trades(edge_on_high=True)
    few = [t for t in trades if t.signal_date in set(panels.dates[21:24])]

    result = evaluate_score_direction(few, panels)

    assert result["eligible"] is False
    assert result["ranked"] is True
    assert result["split_days"] == 3
    assert str(MIN_DAYS) in result["reason"]


def test_score_direction_separates_unrankable_lane_from_thin_sample() -> None:
    """无排序键的车道不能报成「不足 N 天」—— 那会让人以为等下去就有方向判定。

    生产里 rotation_setup 只作标签、老 trace 缺 watch_score，两者的 trade 全是
    ``ranked=False``。切不出两半 → split_days=0 → 若走天数那条分支，报告会印
    「可切分信号日仅 0 天，不足 20 天」，读起来是攒样本的事，实际攒到明年也一样。
    ``_score_band_summary`` 早就分开了这两种，反向臂必须同口径。
    """
    panels, trades = _direction_panels_and_trades(edge_on_high=True)
    unranked = [_trade(t.signal_date, t.code, score=None, ranked=False) for t in trades]

    result = evaluate_score_direction(unranked, panels)

    assert result["eligible"] is False
    assert result["ranked"] is False
    assert "本层无连续排序键" in result["reason"]
    assert str(MIN_DAYS) not in result["reason"]
    # 报告里也得是这句：读者只看渲染结果,不看 dict。
    text = "\n".join(direction_verdict_lines(score_direction_summary(unranked, panels, horizons=(5,))))
    assert "本层无连续排序键" in text


def test_score_direction_detects_correct_sign() -> None:
    """边缘落在高分半 → 高半显著更好，判「排序键方向正确」。"""
    panels, trades = _direction_panels_and_trades(edge_on_high=True)

    cell = evaluate_score_direction(trades, panels, horizons=(5,))["horizons"]["5"]

    assert cell["days"] >= MIN_DAYS
    assert cell["diff_pct"] > 0
    assert abs(cell["diff_t"]) >= DIRECTION_T_GATE
    assert "排序键方向正确" in cell["verdict"]


def test_score_direction_detects_inverted_sign() -> None:
    """同一批票、同一批边缘，只把边缘挪到低分半 → 判「符号反了」。

    与上一条配成一对：车道级的四栏对这两种摆法给出完全相同的数字，只有这里读得出差别。
    """
    panels, trades = _direction_panels_and_trades(edge_on_high=False)

    cell = evaluate_score_direction(trades, panels, horizons=(5,))["horizons"]["5"]

    assert cell["diff_pct"] < 0
    assert abs(cell["diff_t"]) >= DIRECTION_T_GATE
    assert "排序键符号反了" in cell["verdict"]


def test_score_direction_control_floor_is_narrower_than_a_real_edge() -> None:
    """随机负控制之间的差就是噪声地板。真边缘必须明显超出它，否则这条判据没有分辨力。"""
    panels, trades = _direction_panels_and_trades(edge_on_high=True)

    cell = evaluate_score_direction(trades, panels, horizons=(5,))["horizons"]["5"]

    floor = max(abs(cell["control_diff_min"]), abs(cell["control_diff_max"]))
    assert cell["control_seeds"] >= 2
    assert abs(cell["diff_pct"]) > floor


def test_direction_verdict_calls_both_negative_halves_no_information() -> None:
    """两半都负且差不显著 = 排序键无信息（要删掉这一项），与「符号反了」是相反的动作。"""
    verdict = _direction_verdict(-0.05, 0.4, [0.02, -0.03], high=-1.2, low=-1.15)

    assert "排序键无信息" in verdict


def test_direction_verdict_withholds_when_only_one_half_is_negative() -> None:
    """一正一负而差不显著，只能说方向未定 —— 不是「无信息」，别顺手把这一项删了。"""
    verdict = _direction_verdict(0.05, 0.4, [0.02], high=0.8, low=-0.3)

    assert "方向未定" in verdict
    assert "无信息" not in verdict


def test_direction_verdict_rejects_diff_inside_the_control_floor() -> None:
    """显著但没超地板要单列：那是「量到了但量不准」，样本长起来可能翻成结论。"""
    verdict = _direction_verdict(0.30, 3.5, [0.40, -0.35], high=1.0, low=0.7)

    assert "未超随机负控制离散度" in verdict
    assert "符号反了" not in verdict and "方向正确" not in verdict


def test_direction_verdict_does_not_round_up_the_t_gate() -> None:
    """1.80<=|t|<2.0 明确算没过。贴线放行等于把一次抽样噪声写进排序权重。"""
    assert "方向未定" in _direction_verdict(2.0, 1.99, [0.01], high=1.5, low=-0.5)
    assert "方向正确" in _direction_verdict(2.0, 2.0, [0.01], high=1.5, low=-0.5)


def test_direction_verdict_withholds_when_t_is_unavailable() -> None:
    """方差为零或样本过少时 tstat 返回 None，不能当成 0 去判「不显著」。"""
    assert "算不出 t" in _direction_verdict(0.5, None, [0.01], high=1.0, low=0.5)


def test_direction_verdict_lines_render_each_lane() -> None:
    panels, trades = _direction_panels_and_trades(edge_on_high=True)
    summary = score_direction_summary(trades, panels, horizons=(5,))

    text = "\n".join(direction_verdict_lines(summary))

    assert "## 排序键方向（反向臂）" in text
    assert "pre_breakout" in text
    assert "T+5" in text
    assert "高半" in text and "低半" in text
    assert summary["t_gate"] == DIRECTION_T_GATE
