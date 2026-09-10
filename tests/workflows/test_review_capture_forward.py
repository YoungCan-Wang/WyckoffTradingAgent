"""捕获表前瞻收益评估的检验。

重点不是「函数跑通」，而是四件会静默把结论读反的事：

1. **入场档锚的日期字段不能错。** ``signal_day`` 必须锚 ``previous_trade_date``
   （漏斗 T-1 决策 → T 日开盘进），``post_gap`` 锚 ``trade_date``。锚错了整个窗口
   平移一天，收益仍然算得出来，只是答的不是原来那个问题。
2. **对照池要排除整个复盘池，不只是被测桶。** 其它档位的票同样是当日 >7% 的异动股，
   留在对照池里会把「异动 vs 异动」读成「漏掉的 vs 同侪」。
3. **``signal_day`` 档必须显式拒绝对照。** 该档持有窗口含选样日，对照是循环的；
   缺这一节必须写明「拒绝」，否则会被读成「对照过了」。
4. **fixture 要有分辨力。** 有边缘与无边缘两种输入必须得到不同 verdict——否则这套
   对照接没接上都验不出来（memory control-row-must-measure-itself）。
"""

from __future__ import annotations

import random

import pandas as pd

from core.funnel_effect_eval import MIN_DAYS
from core.funnel_effect_panels import build_panels
from core.funnel_taxonomy import (
    REVIEW_STAGE_CANDIDATE_HIT,
    REVIEW_STAGE_STRENGTH_MISS,
    REVIEW_STAGE_THEME_MISS,
)
from workflows.review_capture_forward import (
    ENTRY_POST_GAP,
    ENTRY_SIGNAL_DAY,
    MERGED_CAPTURED,
    MERGED_MISSED,
    capture_day_map,
    capture_forward_summary,
    evaluate_bucket,
    forward_verdict_lines,
)

# 预热 20 天 + 尾部 T+1+10 的窗口都吃日子，要让 h=10 也过 MIN_DAYS+10 得留够总天数。
DAYS = 95
POOL_SIZE = 40
# 日收益噪声(%)。没有噪声，过去 20 日涨幅就是未来收益的完美代理，配对会把边缘
# 一并杀掉、超额恒为 0，fixture 就只能验 null 那一侧。
NOISE_PCT = 1.5


def _append_series(rows: list[dict[str, object]], code: str, dates: pd.Index, edge_pct: float) -> None:
    rng = random.Random(f"{code}-capture-fixture")
    price = 10.0
    for ds in dates:
        daily = rng.gauss(0.0, NOISE_PCT) + edge_pct
        open_price = price * (1.0 + daily / 200.0)
        price *= 1.0 + daily / 100.0
        rows.append(
            {"code": code, "ds": ds, "open": round(open_price, 4), "close": round(price, 4), "amt_wan": 50000.0}
        )


def _market_frame(*, edge_pct: float, edge_codes: list[str], flat_codes: list[str]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=DAYS).strftime("%Y-%m-%d")
    rows: list[dict[str, object]] = []
    for idx in range(POOL_SIZE):
        _append_series(rows, f"{idx:06d}", dates, 0.0)
    for code in edge_codes:
        _append_series(rows, code, dates, edge_pct)
    for code in flat_codes:
        _append_series(rows, code, dates, 0.0)
    return pd.DataFrame(rows).sort_values(["code", "ds"]).reset_index(drop=True)


def _row(prev_ds: str, ds: str, code: str, stage: str, *, candidate: bool = False) -> dict[str, object]:
    return {
        "trade_date": ds,
        "previous_trade_date": prev_ds,
        "ts_code": code,
        "stage": stage,
        "is_candidate": candidate,
    }


def _fixture(*, edge_pct: float) -> tuple[object, list[dict[str, object]]]:
    """漏掉的票带 edge_pct，另一档位的票不带——用于验证分辨力。"""
    edge_codes = [f"9{idx:05d}" for idx in range(5)]
    flat_codes = [f"8{idx:05d}" for idx in range(4)]
    panels = build_panels(_market_frame(edge_pct=edge_pct, edge_codes=edge_codes, flat_codes=flat_codes))
    # 前 21 天没 mom20；尾部给 post_gap 档的 T+1+10 留够。
    usable = panels.dates[21:-12]
    rows: list[dict[str, object]] = []
    for prev_ds, ds in zip(usable, usable[1:], strict=False):
        rows += [_row(prev_ds, ds, c, REVIEW_STAGE_THEME_MISS) for c in edge_codes]
        rows += [_row(prev_ds, ds, c, REVIEW_STAGE_STRENGTH_MISS) for c in flat_codes]
    return panels, rows


def test_day_map_anchors_previous_trade_date_for_signal_day_entry() -> None:
    """signal_day 锚 previous_trade_date：漏斗 T-1 决策，买在 T 日开盘。"""
    rows = [_row("2026-01-05", "2026-01-06", "000001", REVIEW_STAGE_THEME_MISS)]

    assert capture_day_map(rows, REVIEW_STAGE_THEME_MISS, ENTRY_SIGNAL_DAY) == {
        "2026-01-05": {"formal_l4": ["000001"], "all": ["000001"]}
    }
    assert capture_day_map(rows, REVIEW_STAGE_THEME_MISS, ENTRY_POST_GAP) == {
        "2026-01-06": {"formal_l4": ["000001"], "all": ["000001"]}
    }


def test_day_map_pool_holds_whole_replay_not_just_bucket() -> None:
    """``all`` 必须是整个复盘池：其它档位的票也是当日异动股，不能留在对照池里。"""
    rows = [
        _row("2026-01-05", "2026-01-06", "000001", REVIEW_STAGE_THEME_MISS),
        _row("2026-01-05", "2026-01-06", "000002", REVIEW_STAGE_STRENGTH_MISS),
        _row("2026-01-05", "2026-01-06", "000003", REVIEW_STAGE_CANDIDATE_HIT, candidate=True),
    ]

    day = capture_day_map(rows, REVIEW_STAGE_THEME_MISS, ENTRY_POST_GAP)["2026-01-06"]

    assert day["formal_l4"] == ["000001"]
    assert day["all"] == ["000001", "000002", "000003"]


def test_merged_buckets_split_by_candidate_flag() -> None:
    rows = [
        _row("2026-01-05", "2026-01-06", "000001", REVIEW_STAGE_THEME_MISS),
        _row("2026-01-05", "2026-01-06", "000003", REVIEW_STAGE_CANDIDATE_HIT, candidate=True),
    ]

    missed = capture_day_map(rows, MERGED_MISSED, ENTRY_POST_GAP)["2026-01-06"]
    captured = capture_day_map(rows, MERGED_CAPTURED, ENTRY_POST_GAP)["2026-01-06"]

    assert missed["formal_l4"] == ["000001"]
    assert captured["formal_l4"] == ["000003"]


def test_null_candidate_flag_is_not_treated_as_captured_or_missed_wrongly() -> None:
    """is_candidate=None 是「不知道」。它不该被当成已捕获。"""
    rows = [{"trade_date": "2026-01-06", "previous_trade_date": "2026-01-05", "ts_code": "000001", "stage": "x"}]

    assert capture_day_map(rows, MERGED_CAPTURED, ENTRY_POST_GAP) == {}
    assert capture_day_map(rows, MERGED_MISSED, ENTRY_POST_GAP)["2026-01-06"]["formal_l4"] == ["000001"]


def test_signal_day_entry_refuses_control_but_still_reports_absolute() -> None:
    """含选样日那档：对照必须写明「拒绝」，绝对收益照出。"""
    panels, rows = _fixture(edge_pct=0.0)

    block = evaluate_bucket(rows, panels, REVIEW_STAGE_THEME_MISS, entry=ENTRY_SIGNAL_DAY, horizons=(3,))
    cell = block["horizons"]["3"]

    assert block["control_valid"] is False
    assert "拒绝" in block["control_refusal"]
    assert "matched" not in cell
    assert "control_gap" not in cell
    assert cell["absolute"]["net_pct"] is not None


def test_insufficient_days_says_not_yet_knowable_not_failed() -> None:
    """样本不足是「尚未可知」，不是「未通过」。"""
    panels, rows = _fixture(edge_pct=0.0)
    trimmed = [r for r in rows if r["trade_date"] in set(panels.dates[22:27])]

    cell = evaluate_bucket(trimmed, panels, REVIEW_STAGE_THEME_MISS, horizons=(3,))["horizons"]["3"]

    assert "尚未可知" in cell["verdict"]
    assert "matched" not in cell


def test_min_days_threshold_scales_with_horizon() -> None:
    """h=10 要多十个交易日才够同样多的可评估日，门槛必须逐档递增。"""
    panels, rows = _fixture(edge_pct=0.0)

    block = evaluate_bucket(rows, panels, REVIEW_STAGE_THEME_MISS, horizons=(1, 10))

    assert block["horizons"]["1"]["min_days"] == MIN_DAYS + 1
    assert block["horizons"]["10"]["min_days"] == MIN_DAYS + 10


def test_control_discriminates_edge_from_flat() -> None:
    """有边缘与无边缘必须得到不同结论——否则这套对照接没接上都验不出来。

    两侧用同一个桶（题材共振不足）、同一批日期，唯一差别是那批票有没有独立于动量
    的日边缘。有边缘那侧配对超额应为正且跑赢随机负控制；无边缘那侧不应如此。
    """
    edge_panels, edge_rows = _fixture(edge_pct=0.6)
    flat_panels, flat_rows = _fixture(edge_pct=0.0)

    edge = evaluate_bucket(edge_rows, edge_panels, REVIEW_STAGE_THEME_MISS, horizons=(3,))["horizons"]["3"]
    flat = evaluate_bucket(flat_rows, flat_panels, REVIEW_STAGE_THEME_MISS, horizons=(3,))["horizons"]["3"]

    assert edge["matched"]["excess_pct"] > flat["matched"]["excess_pct"]
    assert edge["absolute"]["net_pct"] > flat["absolute"]["net_pct"]
    # 分辨力的硬要求：两侧 verdict 不能相同。
    assert edge["control_gap"]["verdict"] != flat["control_gap"]["verdict"]


def test_summary_reports_unavailable_without_panels() -> None:
    """缺面板要显式说没出——不能让「没这一节」被读成「过了」。"""
    summary = capture_forward_summary([_row("2026-01-05", "2026-01-06", "000001", "x")], None)

    assert summary["available"] is False
    assert "无行情面板" in summary["reason"]
    assert "未出" in "\n".join(forward_verdict_lines(summary))


def test_verdict_lines_cover_both_entries_and_carry_refusal() -> None:
    panels, rows = _fixture(edge_pct=0.3)

    text = "\n".join(forward_verdict_lines(capture_forward_summary(rows, panels, horizons=(3,))))

    assert "T 日开盘入场" in text
    assert "T+1 开盘入场" in text
    assert "股级胜率" in text
