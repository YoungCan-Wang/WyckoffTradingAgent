"""Evaluate whether the best Backtest Grid parameters sit on a stable neighborhood."""

from __future__ import annotations

from typing import Any

from core.backtest_grid_ranking import RobustParamScore, rank_robust_params
from workflows.backtest_market_report_artifacts import GridCell

ParamKey = tuple[str, int, int, int, int]
REQUIRED_PERIODS = frozenset({"recent_6m", "bull_2020", "bear_2022", "sideways_2023", "volatile_2024"})


def build_parameter_stability(cells: list[GridCell], *, run_url: str = "", generated_at: str = "") -> dict[str, Any]:
    ranked = _rank(cells)
    if not ranked:
        raise ValueError("未找到可评估的回测参数")
    anchor = ranked[0]
    neighbors = _neighbors(anchor, ranked)
    stable = [score for score in neighbors if _cross_period_positive(score)]
    verdict = _verdict(anchor, neighbors, stable)
    return {
        "status": verdict,
        "source": "backtest_grid_parameter_neighborhood",
        "generated_at": generated_at,
        "run_url": run_url,
        "summary": _summary(verdict, anchor, neighbors, stable),
        "anchor": _score_payload(anchor),
        "neighbor_count": len(neighbors),
        "stable_neighbor_count": len(stable),
        "stable_neighbor_ratio": round(len(stable) / len(neighbors), 4) if neighbors else 0.0,
        "neighbors": [_score_payload(score) for score in neighbors],
        "rules": {
            "minimum_neighbors": 2,
            "minimum_stable_ratio": 0.5,
            "required_periods": sorted(REQUIRED_PERIODS),
            "stable_definition": "相邻参数完整覆盖近期、牛市、熊市、震荡市和高波动市且现金收益全部为正",
        },
    }


def _rank(cells: list[GridCell]) -> list[RobustParamScore[GridCell]]:
    return rank_robust_params(
        cells,
        key_fn=_param_key,
        period_fn=lambda cell: cell.period_key or f"{cell.start}_{cell.end}",
        value_fn=lambda cell: cell.cash_total_return,
        representative_fn=_representative,
        period_rank_fn=lambda period: (_period_rank(period), period),
    )


def _param_key(cell: GridCell) -> ParamKey:
    return (cell.portfolio_style or "slot_equal_4", cell.hold, cell.stop_loss, cell.take_profit, cell.trailing_stop)


def _representative(cells: list[GridCell]) -> GridCell:
    preferred = [cell for cell in cells if cell.period_key in {"recent_2m", "recent_6m"}]
    return max(
        preferred or cells,
        key=lambda cell: cell.cash_total_return if cell.cash_total_return is not None else float("-inf"),
    )


def _period_rank(period: str) -> int:
    order = {
        "recent_2m": 0,
        "recent_6m": 1,
        "bull_2020": 2,
        "bear_2022": 3,
        "sideways_2023": 4,
        "volatile_2024": 5,
    }
    return order.get(period, 9)


def _neighbors(
    anchor: RobustParamScore[GridCell],
    ranked: list[RobustParamScore[GridCell]],
) -> list[RobustParamScore[GridCell]]:
    candidates = [score for score in ranked if score.key != anchor.key and score.key[0] == anchor.key[0]]
    value_sets = [sorted({score.key[idx] for score in ranked if score.key[0] == anchor.key[0]}) for idx in range(1, 5)]
    return [score for score in candidates if _adjacent(anchor.key, score.key, value_sets)]


def _adjacent(anchor: ParamKey, candidate: ParamKey, value_sets: list[list[int]]) -> bool:
    differing = [idx for idx in range(1, 5) if anchor[idx] != candidate[idx]]
    if len(differing) != 1:
        return False
    idx = differing[0]
    values = value_sets[idx - 1]
    return abs(values.index(anchor[idx]) - values.index(candidate[idx])) == 1


def _cross_period_positive(score: RobustParamScore[GridCell]) -> bool:
    periods = {cell.period_key for cell in score.cells}
    return (
        REQUIRED_PERIODS.issubset(periods)
        and score.positive_periods == score.period_count
        and min(score.values, default=float("-inf")) > 0
    )


def _verdict(
    anchor: RobustParamScore[GridCell],
    neighbors: list[RobustParamScore[GridCell]],
    stable: list[RobustParamScore[GridCell]],
) -> str:
    if not REQUIRED_PERIODS.issubset({cell.period_key for cell in anchor.cells}):
        return "review"
    if not _cross_period_positive(anchor):
        return "fail"
    if len(neighbors) < 2:
        return "review"
    return "pass" if len(stable) / len(neighbors) >= 0.5 else "fail"


def _summary(
    verdict: str,
    anchor: RobustParamScore[GridCell],
    neighbors: list[RobustParamScore[GridCell]],
    stable: list[RobustParamScore[GridCell]],
) -> str:
    if verdict == "pass":
        return f"最优参数跨周期全正，且 {len(stable)}/{len(neighbors)} 个相邻参数同样全正。"
    missing = REQUIRED_PERIODS - {cell.period_key for cell in anchor.cells}
    if missing:
        return f"最优参数缺少 {', '.join(sorted(missing))} 周期结果，参数稳定性保留人工复核。"
    if not _cross_period_positive(anchor):
        return "最优参数自身未能跨周期全正，参数稳定性不通过。"
    if len(neighbors) < 2:
        return f"仅找到 {len(neighbors)} 个相邻参数，覆盖不足，保留人工复核。"
    return f"仅 {len(stable)}/{len(neighbors)} 个相邻参数跨周期全正，存在参数孤岛风险。"


def _score_payload(score: RobustParamScore[GridCell]) -> dict[str, Any]:
    style, hold, stop_loss, take_profit, trailing_stop = score.key
    return {
        "portfolio_style": style,
        "hold_days": hold,
        "stop_loss_pct": -stop_loss if stop_loss else 0,
        "take_profit_pct": take_profit,
        "trailing_stop_pct": -trailing_stop if trailing_stop else 0,
        "period_count": score.period_count,
        "positive_periods": score.positive_periods,
        "avg_cash_return": _round(score.avg_cash_return),
        "min_cash_return": _round(score.min_cash_return),
        "robust_score": _round(score.score),
    }


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None
