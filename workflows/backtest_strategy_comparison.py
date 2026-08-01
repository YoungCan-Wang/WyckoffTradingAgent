"""Strategy ablation report from backtest markdown artifacts."""

from __future__ import annotations

import csv
import glob
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from workflows.backtest_strategy_variants import DEFAULT_COMPARISON_VARIANTS, VARIANT_LABELS

DEFAULT_COMPARISON_PERIODS = ("bull_2020", "bear_2022", "sideways_2023", "volatile_2024", "recent_6m")
MAX_CASH_DRAWDOWN_PCT = 20.0
_DIR_PATTERN = re.compile(
    r"backtest-strategy-"
    r"(?P<period>recent_2m|recent_6m|bull_2020|bear_2022|sideways_2023|volatile_2024|custom)-"
    r"(?P<variant>[A-T])"
    r"(?:-\d+)?$"
)


@dataclass(frozen=True)
class StrategyComparisonRow:
    period: str
    variant: str
    start: str
    end: str
    cash_return: float | None
    cash_drawdown: float | None
    cash_trades: int | None
    win_rate: float | None
    avg_return: float | None
    sharpe: float | None
    trade_keys: tuple[str, ...] = ()
    executed_trades: tuple[StrategyExecutedTrade, ...] = ()


@dataclass(frozen=True)
class StrategyExecutedTrade:
    trigger: str
    regime: str
    exit_reason: str
    ret_pct: float | None
    pnl: float | None
    cost_total: float | None


def load_strategy_comparison_rows(artifacts_dir: Path) -> list[StrategyComparisonRow]:
    rows: list[StrategyComparisonRow] = []
    paths = sorted(Path(path) for path in glob.glob(str(artifacts_dir / "**" / "summary_*.md"), recursive=True))
    for path in paths:
        match = _DIR_PATTERN.search(path.parent.name)
        if not match:
            continue
        content = path.read_text(encoding="utf-8")
        start, end = _date_range(content)
        rows.append(
            StrategyComparisonRow(
                period=match.group("period"),
                variant=match.group("variant"),
                start=start,
                end=end,
                cash_return=_cash_metric(content, "总收益"),
                cash_drawdown=_cash_metric(content, "现金最大回撤"),
                cash_trades=_cash_int_metric(content, "成交笔数"),
                win_rate=_cash_metric(content, "胜率"),
                avg_return=_cash_trade_average(path.parent),
                sharpe=_metric(content, r"夏普比(?:\s*\(Sharpe Ratio\))?"),
                trade_keys=_trade_keys(path.parent),
                executed_trades=_executed_trades(path.parent),
            )
        )
    return rows


def build_strategy_comparison(rows: list[StrategyComparisonRow]) -> dict[str, Any]:
    evaluation_rows = [row for row in rows if row.period in DEFAULT_COMPARISON_PERIODS]
    by_variant = _by_variant(evaluation_rows)
    baseline = _comparison_baseline(by_variant)
    available = {(row.period, row.variant) for row in rows}
    required = {(period, variant) for period in DEFAULT_COMPARISON_PERIODS for variant in DEFAULT_COMPARISON_VARIANTS}
    evaluations = {}
    for variant, values in by_variant.items():
        reference = baseline
        evaluations[variant] = _evaluate_variant(variant, values, by_variant.get(reference, []), reference)
    return {
        "status": "ready" if required.issubset(available) else "incomplete",
        "baseline": baseline,
        "missing_cells": [f"{period}/{variant}" for period, variant in sorted(required - available)],
        "variant_labels": {key: VARIANT_LABELS[key] for key in by_variant if key in VARIANT_LABELS},
        "rows": [_row_payload(row) for row in sorted(rows, key=lambda row: (row.period, row.variant))],
        "evaluations": evaluations,
        "walk_forward": _walk_forward(evaluation_rows),
        "loss_attribution": _loss_attribution(rows),
        "scope": "以 P 为固定基线，分别验证四个不补位信号门控；recent_2m 仅作诊断，不参与晋级判定。",
        "decision_rule": "全部周期现金收益为正、绝对回撤不超过20%，且真实改变交易、胜出过半、平均增量为正、回撤恶化不超过2个百分点。",
    }


def _comparison_baseline(by_variant: dict[str, list[StrategyComparisonRow]]) -> str:
    variants = set(by_variant)
    if set(DEFAULT_COMPARISON_VARIANTS).issubset(variants):
        return "P"
    if "A" in variants:
        return "A"
    if "M" in variants:
        return "M"
    return min(variants, default="P")


def render_strategy_comparison(report: dict[str, Any]) -> str:
    lines = [
        "# 策略 P/Q/R/S/T A股信号门控对比",
        "",
        "固定同一数据快照、P 组 Top-N、确认口径和组合，所有组共用固定退出。",
        "Q/R/S/T 分别拦截一类 P 组信号且不补位；全部为研究策略。",
        "",
        "| 周期 | 组别 | 现金收益 | 现金回撤 | 成交 | 胜率 | 平均单笔 | 夏普 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(_row_line(row) for row in report.get("rows", []))
    if report.get("missing_cells"):
        lines.extend(["", f"- 证据不完整，缺少：{', '.join(report['missing_cells'])}。"])
    lines.extend(
        [
            "",
            "## 相对参照组结论",
            "",
            "| 组别 | 参照 | 共同周期 | 盈利周期 | 暴露周期 | 改变交易 | 胜出 | 平均收益差 | 最大绝对回撤 | 最大回撤恶化 | 判定 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    baseline = str(report.get("baseline") or "P")
    for variant in sorted(key for key in (report.get("evaluations") or {}) if key != baseline):
        item = (report.get("evaluations") or {}).get(variant, {})
        lines.append(
            f"| {variant} | {item.get('reference_variant', 'A')} | {item.get('common_periods', 0)} | "
            f"{item.get('positive_periods', 0)} | {item.get('exposure_periods', 0)} | "
            f"{item.get('changed_trades', 0)} | {item.get('wins', 0)} | "
            f"{_fmt(item.get('mean_return_delta'), '%')} | {_fmt(item.get('max_abs_drawdown'), '%')} | "
            f"{_fmt(item.get('max_drawdown_worsening'), 'pp')} | "
            f"{item.get('status', 'missing')} |"
        )
    lines.extend(_loss_attribution_lines(report.get("loss_attribution") or {}))
    lines.extend(_walk_forward_lines(report.get("walk_forward") or {}))
    return "\n".join(lines) + "\n"


def _by_variant(rows: list[StrategyComparisonRow]) -> dict[str, list[StrategyComparisonRow]]:
    grouped: dict[str, list[StrategyComparisonRow]] = defaultdict(list)
    for row in rows:
        grouped[row.variant].append(row)
    return dict(grouped)


def _evaluate_variant(
    variant: str,
    rows: list[StrategyComparisonRow],
    baseline_rows: list[StrategyComparisonRow],
    reference_variant: str,
) -> dict[str, Any]:
    if variant == reference_variant:
        return {"status": "baseline", "reference_variant": reference_variant, "common_periods": len(rows), "wins": 0}
    baseline = {row.period: row for row in baseline_rows if row.cash_return is not None}
    pairs = [(baseline[row.period], row) for row in rows if row.period in baseline and row.cash_return is not None]
    deltas = [float(row.cash_return) - float(base.cash_return) for base, row in pairs]
    drawdown_worsening = [
        max(abs(float(row.cash_drawdown or 0.0)) - abs(float(base.cash_drawdown or 0.0)), 0.0) for base, row in pairs
    ]
    wins = sum(delta > 0 for delta in deltas)
    positive_periods = sum(float(row.cash_return or 0.0) > 0 for _, row in pairs)
    max_abs_drawdown = max((abs(float(row.cash_drawdown or 0.0)) for _, row in pairs), default=None)
    exposure = [_trade_delta(base, row) for base, row in pairs]
    exposure_periods = sum(value > 0 for value in exposure)
    changed_trades = sum(exposure)
    required_wins = math.floor(len(pairs) / 2) + 1
    passed = (
        len(pairs) >= 2
        and exposure_periods >= 2
        and wins >= required_wins
        and mean(deltas) > 0
        and positive_periods == len(pairs)
        and max_abs_drawdown is not None
        and max_abs_drawdown <= MAX_CASH_DRAWDOWN_PCT
        and max(drawdown_worsening, default=0) <= 2
    )
    status = "no_effect" if changed_trades == 0 else "pass" if passed else "review"
    return {
        "status": status if len(pairs) >= 2 else "insufficient",
        "reference_variant": reference_variant,
        "common_periods": len(pairs),
        "exposure_periods": exposure_periods,
        "changed_trades": changed_trades,
        "wins": wins,
        "positive_periods": positive_periods,
        "max_abs_drawdown": max_abs_drawdown,
        "mean_return_delta": mean(deltas) if deltas else None,
        "max_drawdown_worsening": max(drawdown_worsening, default=None),
    }


def _walk_forward(rows: list[StrategyComparisonRow]) -> dict[str, Any]:
    grouped: dict[str, list[StrategyComparisonRow]] = defaultdict(list)
    for row in rows:
        if row.cash_return is not None and row.period != "recent_2m":
            grouped[row.period].append(row)
    periods = sorted(grouped, key=lambda key: max((row.end for row in grouped[key]), default=""))
    windows = []
    for train, test in zip(periods, periods[1:], strict=False):
        selected = max(grouped[train], key=lambda row: float(row.cash_return or float("-inf")))
        test_row = next((row for row in grouped[test] if row.variant == selected.variant), None)
        windows.append(
            {
                "train_period": train,
                "test_period": test,
                "selected_variant": selected.variant,
                "train_return": selected.cash_return,
                "test_return": test_row.cash_return if test_row else None,
            }
        )
    positive = sum(float(row["test_return"]) > 0 for row in windows if row["test_return"] is not None)
    evaluated = sum(row["test_return"] is not None for row in windows)
    return {"status": "pass" if evaluated >= 2 and positive == evaluated else "review", "windows": windows}


def _walk_forward_lines(result: dict[str, Any]) -> list[str]:
    lines = ["", "## Walk-forward", ""]
    for row in result.get("windows", []):
        lines.append(
            f"- {row['train_period']} 选出 {row['selected_variant']}，在 {row['test_period']} 的现金收益为 "
            f"{_fmt(row.get('test_return'), '%')}。"
        )
    lines.append(f"- 判定：{result.get('status', 'review')}。")
    return lines


def _loss_attribution(rows: list[StrategyComparisonRow]) -> dict[str, Any]:
    result = {}
    for variant in DEFAULT_COMPARISON_VARIANTS:
        selected = [row for row in rows if row.variant == variant and row.period in DEFAULT_COMPARISON_PERIODS]
        trades = [trade for row in selected for trade in row.executed_trades]
        result[variant] = {
            "source": "executed_cash_trades",
            "covered_periods": sorted({row.period for row in selected if row.executed_trades}),
            "trade_count": len(trades),
            "by_trigger": _group_executed_trades(trades, "trigger"),
            "by_regime": _group_executed_trades(trades, "regime"),
            "by_exit_reason": _group_executed_trades(trades, "exit_reason"),
        }
    return result


def _group_executed_trades(trades: list[StrategyExecutedTrade], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[StrategyExecutedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[str(getattr(trade, field) or "unknown")].append(trade)
    return sorted((_executed_trade_stats(key, values) for key, values in grouped.items()), key=_attribution_sort_key)


def _executed_trade_stats(group: str, trades: list[StrategyExecutedTrade]) -> dict[str, Any]:
    returns = [value for trade in trades if (value := trade.ret_pct) is not None]
    pnl = sum(value for trade in trades if (value := trade.pnl) is not None)
    capital = sum(value for trade in trades if (value := trade.cost_total) is not None)
    return {
        "group": group,
        "trades": len(trades),
        "win_rate": sum(value > 0 for value in returns) / len(returns) * 100.0 if returns else None,
        "avg_return": mean(returns) if returns else None,
        "pnl": pnl,
        "pnl_on_cost_pct": pnl / capital * 100.0 if capital > 0 else None,
    }


def _attribution_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    return float(row.get("pnl") or 0.0), str(row.get("group") or "")


def _loss_attribution_lines(result: dict[str, Any]) -> list[str]:
    selected = result.get("P") or {}
    lines = ["", "## P 组实际成交亏损归因", ""]
    if not selected.get("trade_count"):
        return [*lines, "- 缺少现金成交明细，无法归因。"]
    lines.extend(
        [
            "只列每个维度亏损贡献最大的三组；`资本盈亏率` 为该组总盈亏 / 累计买入成本，不等于组合收益。",
            "",
            "| 维度 | 分组 | 成交 | 胜率 | 平均单笔 | 总盈亏(元) | 资本盈亏率 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for label, key in (("信号", "by_trigger"), ("水温", "by_regime"), ("退出", "by_exit_reason")):
        losses = [row for row in selected.get(key, []) if float(row.get("pnl") or 0.0) < 0][:3]
        lines.extend(_attribution_line(label, row) for row in losses)
    return lines


def _attribution_line(label: str, row: dict[str, Any]) -> str:
    return (
        f"| {label} | {row.get('group', 'unknown')} | {row.get('trades', 0)} | "
        f"{_fmt(row.get('win_rate'), '%')} | {_fmt(row.get('avg_return'), '%')} | "
        f"{_fmt(row.get('pnl'))} | {_fmt(row.get('pnl_on_cost_pct'), '%')} |"
    )


def _row_line(row: dict[str, Any]) -> str:
    return (
        f"| {row['period']} | {row['variant']} | {_fmt(row.get('cash_return'), '%')} | "
        f"{_fmt(row.get('cash_drawdown'), '%')} | {row.get('cash_trades') or 0} | "
        f"{_fmt(row.get('win_rate'), '%')} | {_fmt(row.get('avg_return'), '%')} | {_fmt(row.get('sharpe'))} |"
    )


def _row_payload(row: StrategyComparisonRow) -> dict[str, Any]:
    return {
        "period": row.period,
        "variant": row.variant,
        "start": row.start,
        "end": row.end,
        "cash_return": row.cash_return,
        "cash_drawdown": row.cash_drawdown,
        "cash_trades": row.cash_trades,
        "win_rate": row.win_rate,
        "avg_return": row.avg_return,
        "sharpe": row.sharpe,
        "selected_trade_count": len(row.trade_keys),
    }


def _trade_keys(directory: Path) -> tuple[str, ...]:
    paths = sorted(directory.glob("cash_trades_*.csv")) or sorted(directory.glob("trades_*.csv"))
    if not paths:
        return ()
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return tuple(
            sorted(
                f"{row.get('signal_date', '')}:{row.get('code', '')}:"
                f"{_weight_key(row.get('entry_weight_multiplier'))}:{row.get('exit_date', '')}"
                for row in rows
            )
        )


def _weight_key(raw: object) -> str:
    try:
        return f"{float(raw):.4f}" if raw not in (None, "") else "1.0000"
    except (TypeError, ValueError):
        return "1.0000"


def _cash_trade_average(directory: Path) -> float | None:
    paths = sorted(directory.glob("cash_trades_*.csv"))
    if not paths:
        return None
    values: list[float] = []
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                values.append(float(row.get("ret_pct", "")))
            except (TypeError, ValueError):
                continue
    return mean(values) if values else None


def _executed_trades(directory: Path) -> tuple[StrategyExecutedTrade, ...]:
    paths = sorted(directory.glob("cash_trades_*.csv"))
    if not paths:
        return ()
    regimes = _trade_regime_lookup(directory)
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        return tuple(_executed_trade(row, regimes) for row in csv.DictReader(handle))


def _executed_trade(row: dict[str, str], regimes: dict[tuple[str, ...], str]) -> StrategyExecutedTrade:
    key = tuple(str(row.get(field) or "") for field in ("signal_date", "entry_date", "exit_date", "code"))
    return StrategyExecutedTrade(
        trigger=str(row.get("trigger") or "unknown"),
        regime=str(row.get("regime") or regimes.get(key) or "unknown"),
        exit_reason=str(row.get("exit_reason") or "unknown"),
        ret_pct=_optional_float(row.get("ret_pct")),
        pnl=_optional_float(row.get("pnl")),
        cost_total=_optional_float(row.get("cost_total")),
    )


def _trade_regime_lookup(directory: Path) -> dict[tuple[str, ...], str]:
    paths = sorted(directory.glob("trades_*.csv"))
    if not paths:
        return {}
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        return {
            tuple(str(row.get(field) or "") for field in ("signal_date", "entry_date", "exit_date", "code")): str(
                row.get("regime") or "unknown"
            )
            for row in csv.DictReader(handle)
        }


def _optional_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _trade_delta(base: StrategyComparisonRow, candidate: StrategyComparisonRow) -> int:
    return len(set(base.trade_keys).symmetric_difference(candidate.trade_keys))


def _line_value(content: str, label: str) -> str | None:
    pattern = re.compile(rf"^\s*-\s*{label}\s*:\s*(.+?)\s*$")
    return next((match.group(1) for line in content.splitlines() if (match := pattern.match(line))), None)


def _metric(content: str, label: str) -> float | None:
    raw = _line_value(content, label)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw or "")
    return float(match.group(0)) if match else None


def _cash_section(content: str) -> str:
    marker = "## 真实现金账户模拟"
    if marker not in content:
        return content
    section = content.split(marker, 1)[1]
    return section.split("\n## ", 1)[0]


def _cash_metric(content: str, label: str) -> float | None:
    return _metric(_cash_section(content), label)


def _cash_int_metric(content: str, label: str) -> int | None:
    value = _cash_metric(content, label)
    return int(value) if value is not None else None


def _date_range(content: str) -> tuple[str, str]:
    raw = _line_value(content, "区间") or ""
    parts = [part.strip() for part in raw.split("~", 1)]
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "")


def _fmt(value: object, suffix: str = "") -> str:
    return "-" if value is None else f"{float(value):+.2f}{suffix}"
