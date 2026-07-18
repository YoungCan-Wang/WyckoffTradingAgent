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

_DIR_PATTERN = re.compile(
    r"backtest-strategy-(?P<period>recent_2m|recent_6m|bull_2020|bear_2022|custom)-(?P<variant>[A-I])"
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
                avg_return=_metric(content, "平均收益"),
                sharpe=_metric(content, r"夏普比(?:\s*\(Sharpe Ratio\))?"),
                trade_keys=_trade_keys(path.parent),
            )
        )
    return rows


def build_strategy_comparison(rows: list[StrategyComparisonRow]) -> dict[str, Any]:
    by_variant = _by_variant(rows)
    evaluations = {
        variant: _evaluate_variant(variant, values, by_variant.get("A", [])) for variant, values in by_variant.items()
    }
    return {
        "status": "ready" if set(DEFAULT_COMPARISON_VARIANTS).issubset(by_variant) else "incomplete",
        "baseline": "A",
        "variant_labels": {key: VARIANT_LABELS[key] for key in by_variant if key in VARIANT_LABELS},
        "rows": [_row_payload(row) for row in sorted(rows, key=lambda row: (row.period, row.variant))],
        "evaluations": evaluations,
        "walk_forward": _walk_forward(rows),
        "scope": "默认比较 A/F/G/H/I，只改变 confirmed-only 入场筛选或排序；持仓期统一使用固定退出。",
        "decision_rule": "至少两个周期真实改变入选交易、胜出周期过半、平均收益增量为正，且最大回撤恶化不超过2个百分点。",
    }


def render_strategy_comparison(report: dict[str, Any]) -> str:
    lines = [
        "# 策略 A/F/G/H/I A股实证消融对比",
        "",
        "固定同一数据快照、确认口径、组合与退出参数，仅切换 confirmed-only 入场能力。A 为基线。",
        "F/G 验证弱信号剔除，H 验证 NEUTRAL 广度闸门，I 验证跨触发器分数校准。",
        "",
        "| 周期 | 组别 | 现金收益 | 现金回撤 | 成交 | 胜率 | 平均单笔 | 夏普 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(_row_line(row) for row in report.get("rows", []))
    lines.extend(
        [
            "",
            "## 相对 A 组结论",
            "",
            "| 组别 | 共同周期 | 暴露周期 | 改变交易 | 胜出 | 平均收益差 | 最大回撤恶化 | 判定 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for variant in sorted(key for key in (report.get("evaluations") or {}) if key != "A"):
        item = (report.get("evaluations") or {}).get(variant, {})
        lines.append(
            f"| {variant} | {item.get('common_periods', 0)} | {item.get('exposure_periods', 0)} | "
            f"{item.get('changed_trades', 0)} | {item.get('wins', 0)} | "
            f"{_fmt(item.get('mean_return_delta'), '%')} | {_fmt(item.get('max_drawdown_worsening'), 'pp')} | "
            f"{item.get('status', 'missing')} |"
        )
    lines.extend(_walk_forward_lines(report.get("walk_forward") or {}))
    return "\n".join(lines) + "\n"


def _by_variant(rows: list[StrategyComparisonRow]) -> dict[str, list[StrategyComparisonRow]]:
    grouped: dict[str, list[StrategyComparisonRow]] = defaultdict(list)
    for row in rows:
        grouped[row.variant].append(row)
    return dict(grouped)


def _evaluate_variant(
    variant: str, rows: list[StrategyComparisonRow], baseline_rows: list[StrategyComparisonRow]
) -> dict[str, Any]:
    if variant == "A":
        return {"status": "baseline", "common_periods": len(rows), "wins": 0}
    baseline = {row.period: row for row in baseline_rows if row.cash_return is not None}
    pairs = [(baseline[row.period], row) for row in rows if row.period in baseline and row.cash_return is not None]
    deltas = [float(row.cash_return) - float(base.cash_return) for base, row in pairs]
    drawdown_worsening = [
        max(abs(float(row.cash_drawdown or 0.0)) - abs(float(base.cash_drawdown or 0.0)), 0.0) for base, row in pairs
    ]
    wins = sum(delta > 0 for delta in deltas)
    exposure = [_trade_delta(base, row) for base, row in pairs]
    exposure_periods = sum(value > 0 for value in exposure)
    changed_trades = sum(exposure)
    required_wins = math.floor(len(pairs) / 2) + 1
    passed = (
        len(pairs) >= 2
        and exposure_periods >= 2
        and wins >= required_wins
        and mean(deltas) > 0
        and max(drawdown_worsening, default=0) <= 2
    )
    status = "no_effect" if changed_trades == 0 else "pass" if passed else "review"
    return {
        "status": status if len(pairs) >= 2 else "insufficient",
        "common_periods": len(pairs),
        "exposure_periods": exposure_periods,
        "changed_trades": changed_trades,
        "wins": wins,
        "mean_return_delta": mean(deltas) if deltas else None,
        "max_drawdown_worsening": max(drawdown_worsening, default=None),
    }


def _walk_forward(rows: list[StrategyComparisonRow]) -> dict[str, Any]:
    grouped: dict[str, list[StrategyComparisonRow]] = defaultdict(list)
    for row in rows:
        if row.cash_return is not None:
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
    paths = sorted(directory.glob("trades_*.csv"))
    if not paths:
        return ()
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return tuple(sorted(f"{row.get('signal_date', '')}:{row.get('code', '')}" for row in rows))


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
