"""Walk-forward validation over chronological Backtest Grid periods."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from workflows.backtest_market_report_artifacts import GridCell

ParamKey = tuple[int, int, int, int]


def build_walk_forward_validation(
    cells: list[GridCell], *, run_url: str = "", generated_at: str = ""
) -> dict[str, Any]:
    windows = _walk_forward_windows(cells)
    evaluated = [row for row in windows if row["test_cash_return"] is not None]
    positive = sum(float(row["test_cash_return"]) > 0 for row in evaluated)
    status = "review" if len(evaluated) < 2 else ("pass" if positive == len(evaluated) else "fail")
    return {
        "status": status,
        "source": "backtest_grid_walk_forward",
        "generated_at": generated_at,
        "run_url": run_url,
        "summary": _summary(status, positive, len(evaluated)),
        "window_count": len(windows),
        "evaluated_window_count": len(evaluated),
        "positive_test_count": positive,
        "windows": windows,
        "scope": "本网格验证持有期和退出参数；触发器阈值由 Backtest Trigger Calibration 单独标定。",
    }


def _period_identity(cell: GridCell) -> str:
    """period_key 缺失时按日期区间兜底，别让周期整段从时序链里消失。

    2026-08-21 加 bull_2025 时 _parse_period_key 未同步，这里原来写
    ``if cell.period_key`` 直接过滤掉空 key，于是 run 32537955220 的
    walk_forward 只有 4 个迁移窗口，bull_2025 出现 0 次——而它是最近的完整年度、
    也是那次唯一 +29.78% 的周期。判定「1/16 窗口为正」正是在缺它的链上算出来的。
    """
    if cell.period_key:
        return cell.period_key
    return f"{cell.start}_{cell.end}" if (cell.start or cell.end) else ""


def _walk_forward_windows(cells: list[GridCell]) -> list[dict[str, Any]]:
    by_style_period: dict[tuple[str, str], list[GridCell]] = defaultdict(list)
    period_end: dict[str, str] = {}
    for cell in cells:
        identity = _period_identity(cell)
        if identity and cell.cash_total_return is not None:
            style = cell.portfolio_style or "slot_equal_4"
            by_style_period[(style, identity)].append(cell)
            period_end[identity] = max(period_end.get(identity, ""), cell.end)
    periods = sorted(period_end, key=lambda period: (period_end[period], period))
    styles = sorted({style for style, _period in by_style_period})
    return [
        row
        for style in styles
        for train, test in zip(periods, periods[1:], strict=False)
        if (row := _evaluate_window(style, train, test, by_style_period)) is not None
    ]


def _evaluate_window(
    style: str,
    train_period: str,
    test_period: str,
    grouped: dict[tuple[str, str], list[GridCell]],
) -> dict[str, Any] | None:
    train_cells = grouped.get((style, train_period), [])
    test_cells = grouped.get((style, test_period), [])
    if not train_cells or not test_cells:
        return None
    selected = max(train_cells, key=lambda cell: float(cell.cash_total_return or float("-inf")))
    key = _param_key(selected)
    test_match = next((cell for cell in test_cells if _param_key(cell) == key), None)
    return {
        "portfolio_style": style,
        "train_period": train_period,
        "test_period": test_period,
        "selected_params": _params_payload(selected),
        "train_cash_return": selected.cash_total_return,
        "test_cash_return": test_match.cash_total_return if test_match else None,
        "matched_in_test": test_match is not None,
    }


def _param_key(cell: GridCell) -> ParamKey:
    return cell.hold, cell.stop_loss, cell.take_profit, cell.trailing_stop


def _params_payload(cell: GridCell) -> dict[str, int]:
    return {
        "hold_days": cell.hold,
        "stop_loss_pct": -cell.stop_loss if cell.stop_loss else 0,
        "take_profit_pct": cell.take_profit,
        "trailing_stop_pct": -cell.trailing_stop if cell.trailing_stop else 0,
    }


def _summary(status: str, positive: int, evaluated: int) -> str:
    if status == "review":
        return f"仅 {evaluated} 个滚动窗口具备同参数样本，覆盖不足，保留人工复核。"
    if status == "pass":
        return f"训练期选出的参数在 {positive}/{evaluated} 个后续测试窗口收益为正。"
    return f"训练期最优参数仅在 {positive}/{evaluated} 个后续测试窗口收益为正，存在样本外失效。"
