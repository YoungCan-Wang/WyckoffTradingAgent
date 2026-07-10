from __future__ import annotations

from dataclasses import dataclass

from core.backtest_grid_ranking import rank_robust_params, robust_label, weak_period_guardrails


@dataclass(frozen=True)
class Cell:
    style: str
    period: str
    hold: int
    cash_return: float | None


def test_rank_robust_params_prefers_cross_period_positive_combo() -> None:
    cells = [
        Cell("fast", "recent_6m", 10, 40.0),
        Cell("fast", "bull_2020", 10, -20.0),
        Cell("fast", "bear_2022", 10, -10.0),
        Cell("steady", "recent_6m", 15, 8.0),
        Cell("steady", "bull_2020", 15, 5.0),
        Cell("steady", "bear_2022", 15, 3.0),
    ]

    ranked = rank_robust_params(
        cells,
        key_fn=lambda c: (c.style, c.hold),
        period_fn=lambda c: c.period,
        value_fn=lambda c: c.cash_return,
        representative_fn=lambda group: max(group, key=lambda c: c.cash_return or float("-inf")),
    )

    assert ranked[0].best_cell.style == "steady"
    assert ranked[0].positive_periods == 3
    assert robust_label(ranked[0]) == "稳健参数（跨周期全正）"


def test_rank_robust_params_accepts_workflow_dict_cells() -> None:
    cells = [
        {"style": "confirmation_only", "period": "recent_6m", "hold": 15, "cash_return": 45.98},
        {"style": "confirmation_only", "period": "bull_2020", "hold": 15, "cash_return": 23.09},
        {"style": "confirmation_only", "period": "bear_2022", "hold": 15, "cash_return": -23.40},
    ]

    ranked = rank_robust_params(
        cells,
        key_fn=lambda c: (c["style"], c["hold"]),
        period_fn=lambda c: str(c["period"]),
        value_fn=lambda c: c["cash_return"],
        representative_fn=lambda group: group[0],
    )

    assert robust_label(ranked[0]) == "风险折中参数（熊市未通过）"
    assert ranked[0].min_cash_return == -23.40


def test_rank_robust_params_uses_first_available_recent_period() -> None:
    cells = [
        Cell("fast", "recent_2m", 5, 8.0),
        Cell("fast", "bull_2020", 5, 3.0),
    ]

    ranked = rank_robust_params(
        cells,
        key_fn=lambda c: (c.style, c.hold),
        period_fn=lambda c: c.period,
        value_fn=lambda c: c.cash_return,
        representative_fn=lambda group: group[0],
        period_rank_fn=lambda period: (0 if period == "recent_2m" else 1, period),
    )

    assert ranked[0].recent_cash_return == 8.0


def test_weak_period_guardrails_reports_only_non_positive_periods() -> None:
    cells = [
        Cell("a", "recent_6m", 10, 5.0),
        Cell("b", "bear_2022", 10, -3.0),
        Cell("c", "bear_2022", 15, -1.0),
    ]

    guards = weak_period_guardrails(cells, period_fn=lambda c: c.period, value_fn=lambda c: c.cash_return)

    assert [(g.period_key, g.best_value) for g in guards] == [("bear_2022", -1.0)]
