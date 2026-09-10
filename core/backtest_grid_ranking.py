"""Shared ranking policy for Backtest Grid reports."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from statistics import mean
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RobustParamScore(Generic[T]):
    key: Hashable
    cells: tuple[T, ...]
    best_cell: T
    score: float
    period_count: int
    positive_periods: int
    avg_cash_return: float | None
    min_cash_return: float | None
    recent_cash_return: float | None
    values: tuple[float, ...]


@dataclass(frozen=True)
class PeriodGuardrail:
    period_key: str
    best_value: float


def rank_robust_params(
    cells: Sequence[T],
    *,
    key_fn: Callable[[T], Hashable],
    period_fn: Callable[[T], str],
    value_fn: Callable[[T], float | None],
    representative_fn: Callable[[list[T]], T],
    recent_period: str | None = None,
    period_rank_fn: Callable[[str], tuple[int, str]] | None = None,
) -> list[RobustParamScore[T]]:
    """按跨周期稳健性给参数组排序。

    ``value_fn`` 虽然是注入的,但这个模块只对「现金收益(百分点,可正可负)」成立,两处写死了:
    ``RobustParamScore`` 的字段叫 ``*_cash_return``,``robust_label()`` 每个分支的文案也都在
    说现金收益。换个量纲进来,排序还能跑,读出来的话是错的。

    换成胜率会退化成常数项,不是"另一种排法"。三处判据都把「> 0」当好(:func:`_robust_score`
    的 ``positive_periods``、:func:`robust_label` 的 ``min_cash_return > 0``、
    :func:`weak_period_guardrails` 的 ``max(values) <= 0``),而胜率没有负数。实测 09-09 那批
    网格:传胜率进来,60 组参数的 ``positive_periods`` 全是 6,权重最大的
    ``positive_periods * 4.0`` 变成人人加 24 的常数;``robust_label`` 把头五名全标成
    「稳健参数（跨周期全正）」,而这五名里头名现金收益 +0.52%、第二名 -5.71%——那句标签说的
    是「六个周期都赚钱」,实际只是「胜率都没到 0 以下」。另外 ``representative_fn`` 在两个
    生产调用方那里都按现金收益挑代表格子,与胜率排序错位(今天两个调用方两处都传现金收益,
    所以还没错位,换量纲才会)。

    要出胜率口径的对照,用 :func:`workflows.backtest_market_report_artifacts.win_rate_span`
    在展示层汇总,别改这里的排序键——改它会一次性改掉所有历史格子的排名。
    """
    groups: dict[Hashable, list[T]] = defaultdict(list)
    for cell in cells:
        groups[key_fn(cell)].append(cell)
    resolved_recent_period = recent_period or _resolve_recent_period(cells, period_fn, period_rank_fn)
    scores = [
        _score_param_group(key, group, period_fn, value_fn, representative_fn, resolved_recent_period)
        for key, group in groups.items()
    ]
    return sorted((s for s in scores if s.values), key=lambda item: item.score, reverse=True)


def robust_label(score: RobustParamScore[object] | None) -> str:
    if score is None:
        return "最优参数（按现金收益）"
    if score.period_count < 3:
        return "候选参数（周期覆盖不足）"
    if (
        score.period_count >= 3
        and score.min_cash_return is not None
        and score.min_cash_return > 0
        and score.positive_periods == score.period_count
    ):
        return "稳健参数（跨周期全正）"
    if score.score > 0 and score.positive_periods >= max(1, score.period_count - 1):
        return "折中参数（跨周期惩罚）"
    return "风险折中参数（熊市未通过）"


def weak_period_guardrails(
    cells: Sequence[T],
    *,
    period_fn: Callable[[T], str],
    value_fn: Callable[[T], float | None],
) -> list[PeriodGuardrail]:
    groups: dict[str, list[float]] = defaultdict(list)
    for cell in cells:
        value = value_fn(cell)
        if value is not None:
            groups[period_fn(cell)].append(value)
    guards = [PeriodGuardrail(period, max(values)) for period, values in groups.items() if values and max(values) <= 0]
    return sorted(guards, key=lambda item: item.period_key)


def _score_param_group(
    key: Hashable,
    group: list[T],
    period_fn: Callable[[T], str],
    value_fn: Callable[[T], float | None],
    representative_fn: Callable[[list[T]], T],
    recent_period: str,
) -> RobustParamScore[T]:
    by_period = _best_cells_by_period(group, period_fn, value_fn)
    values = tuple(value for cell in by_period.values() if (value := value_fn(cell)) is not None)
    recent_cell = by_period.get(recent_period)
    recent_ret = value_fn(recent_cell) if recent_cell is not None else None
    positives = sum(1 for value in values if value > 0)
    return RobustParamScore(
        key=key,
        cells=tuple(group),
        best_cell=representative_fn(group),
        score=_robust_score(values, recent_ret, positives),
        period_count=len(by_period),
        positive_periods=positives,
        avg_cash_return=mean(values) if values else None,
        min_cash_return=min(values) if values else None,
        recent_cash_return=recent_ret,
        values=values,
    )


def _best_cells_by_period(
    group: list[T],
    period_fn: Callable[[T], str],
    value_fn: Callable[[T], float | None],
) -> dict[str, T]:
    by_period: dict[str, T] = {}
    for cell in group:
        key = period_fn(cell)
        if key not in by_period or _rank_value(value_fn(cell)) > _rank_value(value_fn(by_period[key])):
            by_period[key] = cell
    return by_period


def _resolve_recent_period(
    cells: Sequence[T],
    period_fn: Callable[[T], str],
    period_rank_fn: Callable[[str], tuple[int, str]] | None,
) -> str:
    period_keys = {period_fn(cell) for cell in cells}
    if not period_keys:
        return "recent_6m"
    if period_rank_fn is None:
        return "recent_6m" if "recent_6m" in period_keys else sorted(period_keys)[0]
    return min(period_keys, key=period_rank_fn)


def _robust_score(values: tuple[float, ...], recent_ret: float | None, positive_periods: int) -> float:
    if not values:
        return float("-inf")
    return min(values) + mean(values) * 0.35 + (recent_ret or 0.0) * 0.2 + positive_periods * 4.0


def _rank_value(value: float | None) -> float:
    return value if value is not None else float("-inf")
