"""Execution-structure baseline: holding period × stop/target, Wyckoff against random.

Under the production exit (15 days, -8% / +18%) every selection source landed near the
assumed round-trip friction and none separated from ``random`` (#579, #590). This report
treats the execution structure itself as the subject: the same signal ledger is replayed
through 15 / 30 / 60-day holds with and without the asymmetric stop, for the funnel and the
random control, so the execution tax and any selection separation can be read per structure
(#597). Portfolio-level excess (cash return minus the index over the period) is reported
next to per-trade excess because only the former sees turnover.
"""

from __future__ import annotations

import csv
import glob
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from core.backtest_periods import period_rank
from workflows.backtest_gate_baseline import (
    BASELINE_PERIODS,
    EXIT_REASONS,
    GateTrade,
    load_cell_trades,
    trade_protocol,
)
from workflows.backtest_report_stats import cash_int_metric, cash_metric, date_range, fmt

STRATEGIES = ("wyckoff", "random", "sector_rs")
CANDIDATES = tuple(strategy for strategy in STRATEGIES if strategy != "random")
HOLDS = (15, 30, 60)
EXITS = (("sl8_tp18", "-8% / +18%"), ("none", "只到期"))
CELLS = tuple(f"h{hold}_{exit_key}" for hold in HOLDS for exit_key, _ in EXITS)
BENCHMARKS = (("main", "上证"), ("smallcap", "创业板"))
DECISION_RULE = (
    "每个单元是同一份信号台账在不同持有期 × 退出规则下的重放（台账冷却 = 最小持有期 15 日，与 grid 阶段同一机制），"
    "闸门固定 off。逐笔超额看不到换手，所以每个单元同时给组合层超额：周期现金总收益 − 同期指数收益。"
    "判定三件事：random 的合并逐笔超额在哪个单元 CI 含 0 或为正（执行税归零）；哪个单元哪个选股源（wyckoff / "
    "sector_rs）的合并超额 CI 下沿高于 random 的上沿且两基准同向（选股分离）；同持有期下 none 与 sl8_tp18 的 random "
    "差是止损的税，同退出下 h60 与 h15 的组合层差是换手的税。只有同时满足分离且选股源自身 CI 下沿 > 0 的单元才算"
    "扣费后为正的配置。"
)
SHARD_DIR_PATTERN = re.compile(
    r"backtest-exit-"
    r"(?P<period>recent_2m|recent_6m|bull_2020|bear_2022|sideways_2023|volatile_2024|bull_2025|custom)-"
    r"(?P<strategy>wyckoff|random|sector_rs)"
    r"(?:-\d+)?$"
)
CELL_DIR_PATTERN = re.compile(r"backtest-exit-h(?P<hold>\d+)-sl(?P<stop>[\d.]+)-tp(?P<take>[\d.]+)-tr[\d.]+$")


@dataclass(frozen=True)
class ExitCellRow:
    period: str
    strategy: str
    cell: str
    start: str
    end: str
    cash_return: float | None
    cash_trades: int | None
    bench_return: dict[str, float | None]
    trades: tuple[GateTrade, ...] = ()


def load_exit_baseline_rows(artifacts_dir: Path) -> list[ExitCellRow]:
    rows: list[ExitCellRow] = []
    for path in sorted(Path(p) for p in glob.glob(str(artifacts_dir / "**" / "summary_*.md"), recursive=True)):
        cell_dir = path.parent
        cell = _cell_key(cell_dir.name)
        shard = SHARD_DIR_PATTERN.search(cell_dir.parent.name)
        if cell is None or shard is None:
            continue
        content = path.read_text(encoding="utf-8")
        start, end = date_range(content)
        rows.append(
            ExitCellRow(
                period=shard.group("period"),
                strategy=shard.group("strategy"),
                cell=cell,
                start=start,
                end=end,
                cash_return=cash_metric(content, "总收益"),
                cash_trades=cash_int_metric(content, "成交笔数"),
                bench_return={
                    key: _bench_return(cell_dir.parent / f"benchmark_{key}.csv", start, end) for key, _ in BENCHMARKS
                },
                trades=load_cell_trades(cell_dir),
            )
        )
    return rows


def _cell_key(name: str) -> str | None:
    match = CELL_DIR_PATTERN.search(name)
    if not match:
        return None
    exit_key = "none" if float(match.group("stop")) == 0 and float(match.group("take")) == 0 else "sl8_tp18"
    return f"h{int(match.group('hold'))}_{exit_key}"


def _bench_return(path: Path, start: str, end: str) -> float | None:
    """Close-to-close index return over the cell's date range; None when the CSV is missing."""
    if not path.exists():
        return None
    closes: list[tuple[str, float]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            day = _iso_date(str(row.get("date", "")))
            try:
                close = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= day <= end and close > 0:
                closes.append((day, close))
    if len(closes) < 2:
        return None
    closes.sort()
    return (closes[-1][1] / closes[0][1] - 1.0) * 100.0


def _iso_date(raw: str) -> str:
    text = raw.strip()[:10]
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def build_exit_baseline(rows: list[ExitCellRow]) -> dict[str, Any]:
    scoped = [row for row in rows if row.period in BASELINE_PERIODS]
    required = {(s, p, c) for s in STRATEGIES for p in BASELINE_PERIODS for c in CELLS}
    missing = sorted(required - {(row.strategy, row.period, row.cell) for row in scoped})
    pooled = {
        strategy: {
            cell: _cell_summary([r for r in scoped if r.strategy == strategy and r.cell == cell]) for cell in CELLS
        }
        for strategy in STRATEGIES
    }
    separation = {cell: {strategy: _separation(pooled, strategy, cell) for strategy in CANDIDATES} for cell in CELLS}
    return {
        "status": "ready" if not missing else "incomplete",
        "missing_cells": [f"{s}/{p}/{c}" for s, p, c in missing],
        "decision_rule": DECISION_RULE,
        "cells": [_period_payload(row) for row in sorted(scoped, key=_row_order)],
        "pooled": pooled,
        "separation": separation,
        "verdict": _verdict(pooled, separation),
    }


def _row_order(row: ExitCellRow) -> tuple:
    return (STRATEGIES.index(row.strategy), CELLS.index(row.cell), period_rank(row.period))


def _period_payload(row: ExitCellRow) -> dict[str, Any]:
    return {
        "strategy": row.strategy,
        "cell": row.cell,
        "period": row.period,
        "start": row.start,
        "end": row.end,
        "cash_return": row.cash_return,
        "cash_trades": row.cash_trades,
        "portfolio_excess": _portfolio_excess(row),
        **trade_protocol(list(row.trades)),
    }


def _portfolio_excess(row: ExitCellRow) -> dict[str, float | None]:
    return {
        key: None if row.cash_return is None or bench is None else row.cash_return - bench
        for key, bench in row.bench_return.items()
    }


def _cell_summary(rows: list[ExitCellRow]) -> dict[str, Any]:
    trades = [trade for row in rows for trade in row.trades]
    excess = [_portfolio_excess(row) for row in rows]
    portfolio = {
        key: {
            "periods": sum(item[key] is not None for item in excess),
            "positive": sum((item[key] or 0) > 0 for item in excess if item[key] is not None),
            "avg": mean(values) if (values := [item[key] for item in excess if item[key] is not None]) else None,
        }
        for key, _ in BENCHMARKS
    }
    return {"periods": len(rows), "portfolio_excess": portfolio, **trade_protocol(trades)}


def _separation(pooled: dict[str, dict[str, Any]], strategy: str, cell: str) -> str:
    """One selection source versus random on one structure; both benchmarks must agree, overlap is a tie."""
    outcomes = set()
    for key, _ in BENCHMARKS:
        strategy_ci = pooled[strategy][cell][f"excess_{key}"]["ci95"]
        random_ci = pooled["random"][cell][f"excess_{key}"]["ci95"]
        if strategy_ci is None or random_ci is None:
            return "insufficient"
        if strategy_ci[1] < random_ci[0]:
            outcomes.add("below_random")
        elif strategy_ci[0] > random_ci[1]:
            outcomes.add("above_random")
        else:
            outcomes.add("indistinguishable")
    return outcomes.pop() if len(outcomes) == 1 else "indistinguishable"


def _verdict(pooled: dict[str, dict[str, Any]], separation: dict[str, dict[str, str]]) -> dict[str, Any]:
    tax_free = [
        cell for cell in CELLS if pooled["random"][cell]["signal"] != "negative" and pooled["random"][cell]["trades"]
    ]
    above = [
        (strategy, cell) for cell in CELLS for strategy in CANDIDATES if separation[cell][strategy] == "above_random"
    ]
    positive = [f"{strategy}/{cell}" for strategy, cell in above if pooled[strategy][cell]["signal"] == "positive"]
    if positive:
        status = "positive_structure_found"
    elif above:
        status = "separation_without_positive_excess"
    elif tax_free:
        status = "execution_tax_removable_no_selection_signal"
    else:
        status = "execution_tax_persists"
    return {"status": status, "positive_cells": positive, "random_non_negative_cells": tax_free}


def render_exit_baseline(report: dict[str, Any]) -> str:
    lines = [
        "# 执行结构基线：持有期 × 止损有无（wyckoff / sector_rs 对 random，闸门 off）",
        "",
        f"- 状态: {report['status']}",
        f"- 判定: `{report['verdict']['status']}`；扣费后为正的单元: {report['verdict']['positive_cells'] or '无'}；"
        f"random 非负的单元: {report['verdict']['random_non_negative_cells'] or '无'}",
        f"- 判定规则：{report['decision_rule']}",
    ]
    if report["missing_cells"]:
        lines.append(f"- 缺格: {', '.join(report['missing_cells'])}")
    lines += ["", "## 合并六基线周期", ""]
    lines += _pooled_lines(report["pooled"], report["separation"])
    lines += ["", "## 分周期", ""]
    lines += _period_lines(report["cells"])
    return "\n".join(lines) + "\n"


def _pooled_lines(pooled: dict[str, dict[str, Any]], separation: dict[str, str]) -> list[str]:
    lines = [
        "| 单元 | 策略 | 笔数 | 对上证 超额 | 对创业板 超额 | 止损/止盈/到期 | 组合超额 上证 (正/周期) | 组合超额 创业板 (正/周期) | 对 random |",
        "|---|---|---:|---|---|---|---|---|---|",
    ]
    for cell in CELLS:
        for strategy in STRATEGIES:
            item = pooled[strategy][cell]
            lines.append(
                f"| {cell} | {strategy} | {item['trades']} | {_excess_cell(item['excess_main'])} | "
                f"{_excess_cell(item['excess_smallcap'])} | {_exit_cell(item)} | "
                f"{_portfolio_cell(item['portfolio_excess']['main'])} | {_portfolio_cell(item['portfolio_excess']['smallcap'])} | "
                f"{separation[cell].get(strategy, '-')} |"
            )
    return lines


def _period_lines(cells: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 单元 | 策略 | 周期 | 笔数 | 均收 | 对上证 超额 | 对创业板 超额 | 止损/止盈/到期 | 现金收益 | 组合超额 上证 / 创业板 |",
        "|---|---|---|---:|---:|---|---|---|---:|---|",
    ]
    for item in cells:
        excess = item["portfolio_excess"]
        lines.append(
            f"| {item['cell']} | {item['strategy']} | {item['period']} | {item['trades']} | {fmt(item['avg_return'], '%')} | "
            f"{_excess_cell(item['excess_main'])} | {_excess_cell(item['excess_smallcap'])} | {_exit_cell(item)} | "
            f"{fmt(item['cash_return'], '%')} | {fmt(excess['main'], 'pp')} / {fmt(excess['smallcap'], 'pp')} |"
        )
    return lines


def _portfolio_cell(item: dict[str, Any]) -> str:
    return f"{fmt(item['avg'], 'pp')} ({item['positive']}/{item['periods']})"


def _exit_cell(item: dict[str, Any]) -> str:
    share = item.get("exit_reasons") or {}
    return " / ".join("-" if share.get(reason) is None else f"{share[reason]:.0f}%" for reason in EXIT_REASONS)


def _excess_cell(excess: dict[str, Any] | None) -> str:
    excess = excess or {}
    ci = excess.get("ci95")
    return f"{fmt(excess.get('avg'), '%')} {'-' if not ci else f'[{ci[0]:+.2f}, {ci[1]:+.2f}]'}"
