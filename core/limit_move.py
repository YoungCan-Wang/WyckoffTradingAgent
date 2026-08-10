"""涨跌停识别（纯计算层）。

Wyckoff 的 Effort vs Result 假设"跌幅"和"缩量/放量"能反映买卖力量对比，
但 A 股涨跌停制度会截断价格发现：一字跌停当天几乎没有真实换手，
"跌停"本身不代表出货或走弱确认，也不能被当作有效的 Spring 支撑测试。

本模块只做定性识别，不做买卖决策：
- limit_pct: 该股票的涨跌停幅度（A股主板/创业板/科创板/北交所/ST）；港股无涨跌停，返回 None
- classify_limit_move: 结合当日 OHLC + 昨收，判断是否触及涨跌停、是否一字板/烂板/炸板；港股返回 None
"""

from __future__ import annotations

from dataclasses import dataclass

from core.cn_boards import cn_board

_LIMIT_TOUCH_TOLERANCE_PCT = 0.15  # 价格与理论涨跌停价的容差（挂钩四舍五入误差）


def is_st_name(name: str) -> bool:
    """粗略判断是否 ST/*ST（仅基于股票名称前缀，无需额外数据源）。"""
    text = str(name or "").strip().upper()
    return text.startswith("ST") or text.startswith("*ST")


def is_st_risk_warning(code: str, name: str) -> bool:
    """是否属 A 股风险警示（ST/*ST）标的，用于候选池硬过滤。

    ST 是 A 股监管制度，港股/美股没有该制度。这两个市场的 ``name`` 常缺失并回落成
    代码本身，若按名称子串匹配会误伤正常标的（美股 116 只 ticker 含 "ST"，
    如 ``COST``/``FAST``/``STLD``），因此非 A 股代码一律不判。
    """
    if cn_board(code) == "unknown":
        return False
    return is_st_name(name) or "ST" in str(name or "").upper()


def limit_pct(code: str, name: str = "", *, market: str = "cn") -> float | None:
    """返回该股票的涨跌停幅度（如 10.0 表示 ±10%）。

    风险警示（ST）只收窄沪深主板的幅度到 ±5%；创业板、科创板、北交所的 ST 股
    仍按各自板块的幅度执行，不叠加主板的 5% 规则。
    港股和美股没有涨跌停制度，非 A 股市场一律返回 None。
    """
    if market != "cn":
        return None
    board = cn_board(code)
    if board == "bse":
        return 30.0
    if board in {"chinext", "star"}:
        return 20.0
    return 5.0 if is_st_name(name) else 10.0


@dataclass(frozen=True)
class LimitMoveState:
    """单日涨跌停状态快照。"""

    limit_pct: float
    limit_up_price: float
    limit_down_price: float
    touched_limit_up: bool
    touched_limit_down: bool
    closed_limit_up: bool
    closed_limit_down: bool
    one_word_board: bool  # 开盘即封死涨跌停、全天几乎无波动（真一字板）
    opened_then_broke: bool  # 开盘未涨跌停，盘中触及后未能封住（炸板/烂板）


def _round2(value: float) -> float:
    return round(float(value), 2)


def classify_limit_move(
    *,
    code: str,
    name: str,
    prev_close: float,
    open_: float,
    high: float,
    low: float,
    close: float,
    market: str = "cn",
) -> LimitMoveState | None:
    """基于前收盘 + 当日 OHLC 判断涨跌停状态。数据不足时返回 None。

    港股无涨跌停制度，market="hk" 时直接返回 None。
    """
    if prev_close <= 0:
        return None
    pct = limit_pct(code, name, market=market)
    if pct is None:
        return None
    limit_up = _round2(prev_close * (1 + pct / 100.0))
    limit_down = _round2(prev_close * (1 - pct / 100.0))
    tol = pct * _LIMIT_TOUCH_TOLERANCE_PCT / 100.0 * prev_close

    touched_up = high >= limit_up - tol
    touched_down = low <= limit_down + tol
    closed_up = close >= limit_up - tol
    closed_down = close <= limit_down + tol

    day_range = max(high - low, 0.0)
    near_zero_range = day_range <= tol * 2
    one_word_up = closed_up and open_ >= limit_up - tol and near_zero_range
    one_word_down = closed_down and open_ <= limit_down + tol and near_zero_range

    opened_then_broke = (touched_up or touched_down) and not (closed_up or closed_down)

    return LimitMoveState(
        limit_pct=pct,
        limit_up_price=limit_up,
        limit_down_price=limit_down,
        touched_limit_up=touched_up,
        touched_limit_down=touched_down,
        closed_limit_up=closed_up,
        closed_limit_down=closed_down,
        one_word_board=bool(one_word_up or one_word_down),
        opened_then_broke=bool(opened_then_broke),
    )


def describe_limit_move(state: LimitMoveState | None) -> str:
    """生成人类可读的涨跌停状态描述，供诊断文本/LLM prompt 使用。"""
    if state is None:
        return ""
    if state.one_word_board and state.closed_limit_down:
        return f"一字跌停(±{state.limit_pct:.0f}%)，全天几乎无真实换手，不能视为有效缩量/放量信号"
    if state.one_word_board and state.closed_limit_up:
        return f"一字涨停(±{state.limit_pct:.0f}%)，封板惜售，量能参考意义有限"
    if state.closed_limit_down:
        return f"收盘跌停(±{state.limit_pct:.0f}%)，盘中曾打开过，有真实换手"
    if state.closed_limit_up:
        return f"收盘涨停(±{state.limit_pct:.0f}%)"
    if state.touched_limit_down and state.opened_then_broke:
        return "盘中触及跌停后打开（烂板），未能封住"
    if state.touched_limit_up and state.opened_then_broke:
        return "盘中触及涨停后炸板，未能封住"
    return ""


__all__ = [
    "LimitMoveState",
    "classify_limit_move",
    "describe_limit_move",
    "is_st_name",
    "is_st_risk_warning",
    "limit_pct",
]
