"""Execution-gate baseline: the live regime gate versus ``off`` on the same confirmed signals.

Production only opens new positions in the regimes STEP4 allows, so grid cells never show
what the signals earn in the regimes it blocks. This report pairs the two gates per period
and applies one fixed protocol: per-trade excess over both snapshot benchmarks (net of the
backtest friction already inside ``ret_pct``), bootstrap CI, right-tail dependence, and the
marginal trades the gate removes, split by regime.
"""

from __future__ import annotations

import csv
import glob
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from core.backtest_periods import period_rank
from workflows.backtest_report_stats import (
    bootstrap_mean_ci,
    cash_int_metric,
    cash_metric,
    date_range,
    fmt,
    optional_float,
    return_stats,
)

GATES = ("live", "off")
BASELINE_PERIODS = ("bull_2020", "bear_2022", "sideways_2023", "volatile_2024", "bull_2025", "recent_6m")
TAIL_DROPS = (1, 3, 5)
DECISION_RULE = (
    "每个单元的判定只看扣费后超额的 95% bootstrap CI：对上证与创业板指两个基准都不含 0 且同号才记为"
    "「正超额」/「负超额」，否则记「不显著」。任一闸门组在六个周期里不显著或负超额的周期 ≥ 4，"
    "即判该组无跨周期超额；闸门放行组新增的成交（仅 off）必须按水温单独看，不能只看现金收益差。"
)
_DIR_PATTERN = re.compile(
    r"backtest-gate-"
    r"(?P<period>recent_2m|recent_6m|bull_2020|bear_2022|sideways_2023|volatile_2024|bull_2025|custom)-"
    r"(?P<gate>live|off)"
    r"(?:-\d+)?$"
)


@dataclass(frozen=True)
class GateTrade:
    key: str
    regime: str
    ret_pct: float | None
    excess_main_pct: float | None
    excess_smallcap_pct: float | None


@dataclass(frozen=True)
class GateBaselineRow:
    period: str
    gate: str
    start: str
    end: str
    cash_return: float | None
    cash_drawdown: float | None
    cash_trades: int | None
    trades: tuple[GateTrade, ...] = ()


def load_gate_baseline_rows(artifacts_dir: Path, ignored_dirs: list[str] | None = None) -> list[GateBaselineRow]:
    rows: list[GateBaselineRow] = []
    paths = sorted(Path(path) for path in glob.glob(str(artifacts_dir / "**" / "summary_*.md"), recursive=True))
    for path in paths:
        match = _DIR_PATTERN.search(path.parent.name)
        if not match:
            if ignored_dirs is not None:
                ignored_dirs.append(str(path.parent.relative_to(artifacts_dir)))
            continue
        content = path.read_text(encoding="utf-8")
        start, end = date_range(content)
        rows.append(
            GateBaselineRow(
                period=match.group("period"),
                gate=match.group("gate"),
                start=start,
                end=end,
                cash_return=cash_metric(content, "总收益"),
                cash_drawdown=cash_metric(content, "现金最大回撤"),
                cash_trades=cash_int_metric(content, "成交笔数"),
                trades=_load_trades(path.parent),
            )
        )
    return rows


def _load_trades(directory: Path) -> tuple[GateTrade, ...]:
    paths = sorted(directory.glob("trades_*.csv"))
    if not paths:
        return ()
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        return tuple(_trade(row) for row in csv.DictReader(handle))


def _trade(row: dict[str, str]) -> GateTrade:
    ret = optional_float(row.get("ret_pct"))
    main = optional_float(row.get("bench_main_ret_pct"))
    smallcap = optional_float(row.get("bench_smallcap_ret_pct"))
    return GateTrade(
        key=f"{row.get('signal_date', '')}:{row.get('code', '')}",
        regime=str(row.get("regime") or "unknown"),
        ret_pct=ret,
        excess_main_pct=ret - main if ret is not None and main is not None else None,
        excess_smallcap_pct=ret - smallcap if ret is not None and smallcap is not None else None,
    )


def build_gate_baseline(rows: list[GateBaselineRow], ignored_dirs: list[str] | None = None) -> dict[str, Any]:
    by_cell = {(row.period, row.gate): row for row in rows}
    required = {(period, gate) for period in BASELINE_PERIODS for gate in GATES}
    cells = [_cell_payload(row) for row in sorted(rows, key=lambda row: (period_rank(row.period), row.gate))]
    periods = sorted({row.period for row in rows}, key=period_rank)
    marginal: dict[str, dict[str, Any]] = {}
    off_only: list[GateTrade] = []
    for period in periods:
        item = _marginal(by_cell.get((period, "live")), by_cell.get((period, "off")))
        if item is not None:
            off_only.extend(item.pop("off_only_trades"))
            marginal[period] = item
    return {
        "status": "ready" if required.issubset(by_cell) else "incomplete",
        "missing_cells": [f"{period}/{gate}" for period, gate in sorted(required - set(by_cell))],
        "ignored_dirs": sorted(ignored_dirs or []),
        "decision_rule": DECISION_RULE,
        "cells": cells,
        "pooled": {gate: _protocol(_gate_trades(rows, gate)) for gate in GATES},
        "pooled_by_regime": {gate: _by_regime(_gate_trades(rows, gate)) for gate in GATES},
        "marginal": marginal,
        "off_only_by_regime": _by_regime(off_only),
        "verdict": {gate: _verdict([cell for cell in cells if cell["gate"] == gate]) for gate in GATES},
    }


def _gate_trades(rows: list[GateBaselineRow], gate: str) -> list[GateTrade]:
    return [trade for row in rows if row.gate == gate and row.period in BASELINE_PERIODS for trade in row.trades]


def _cell_payload(row: GateBaselineRow) -> dict[str, Any]:
    return {
        "period": row.period,
        "gate": row.gate,
        "start": row.start,
        "end": row.end,
        "cash_return": row.cash_return,
        "cash_drawdown": row.cash_drawdown,
        "cash_trades": row.cash_trades,
        **_protocol(list(row.trades)),
        "by_regime": _by_regime(list(row.trades)),
    }


def _protocol(trades: list[GateTrade]) -> dict[str, Any]:
    returns = [trade.ret_pct for trade in trades if trade.ret_pct is not None]
    main = [trade.excess_main_pct for trade in trades if trade.excess_main_pct is not None]
    smallcap = [trade.excess_smallcap_pct for trade in trades if trade.excess_smallcap_pct is not None]
    ordered = sorted(returns, reverse=True)
    return {
        **return_stats([trade.ret_pct for trade in trades]),
        "ci95": bootstrap_mean_ci(returns),
        "excess_main": _excess(main),
        "excess_smallcap": _excess(smallcap),
        "tail": {f"drop_{drop}": mean(ordered[drop:]) if len(ordered) > drop else None for drop in TAIL_DROPS},
        "signal": _signal(main, smallcap),
    }


def _excess(values: list[float]) -> dict[str, Any]:
    return {
        "trades": len(values),
        "avg": mean(values) if values else None,
        "ci95": bootstrap_mean_ci(values),
        "beat_rate": sum(value > 0 for value in values) / len(values) * 100.0 if values else None,
    }


def _signal(main: list[float], smallcap: list[float]) -> str:
    """Sign of the excess only when both benchmarks' CIs exclude zero on the same side."""
    signs = {_ci_sign(bootstrap_mean_ci(values)) for values in (main, smallcap)}
    if signs == {"positive"}:
        return "positive"
    if signs == {"negative"}:
        return "negative"
    return "inconclusive"


def _ci_sign(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "inconclusive"
    if ci[0] > 0:
        return "positive"
    return "negative" if ci[1] < 0 else "inconclusive"


def _by_regime(trades: list[GateTrade]) -> list[dict[str, Any]]:
    grouped: dict[str, list[GateTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.regime].append(trade)
    result = []
    for regime, items in grouped.items():
        smallcap = [trade.excess_smallcap_pct for trade in items if trade.excess_smallcap_pct is not None]
        result.append(
            {
                "regime": regime,
                **return_stats([trade.ret_pct for trade in items]),
                "excess_smallcap_avg": mean(smallcap) if smallcap else None,
            }
        )
    return sorted(result, key=lambda item: (-item["trades"], item["regime"]))


def _marginal(live: GateBaselineRow | None, off: GateBaselineRow | None) -> dict[str, Any] | None:
    if live is None or off is None or not live.trades or not off.trades:
        return None
    live_map = {trade.key: trade for trade in live.trades}
    off_map = {trade.key: trade for trade in off.trades}
    common = live_map.keys() & off_map.keys()
    off_only = [trade for key, trade in off_map.items() if key not in common]
    return {
        "common": return_stats([off_map[key].ret_pct for key in common]),
        "live_only": return_stats([trade.ret_pct for key, trade in live_map.items() if key not in common]),
        "off_only": return_stats([trade.ret_pct for trade in off_only]),
        "off_only_trades": off_only,
    }


def _verdict(cells: list[dict[str, Any]]) -> dict[str, Any]:
    scoped = [cell for cell in cells if cell["period"] in BASELINE_PERIODS]
    counts = {"positive": 0, "negative": 0, "inconclusive": 0}
    for cell in scoped:
        counts[cell["signal"]] += 1
    not_positive = counts["negative"] + counts["inconclusive"]
    status = "no_cross_period_excess" if not_positive >= 4 else "review" if scoped else "insufficient"
    return {"periods": len(scoped), **counts, "status": status}


def render_gate_baseline(report: dict[str, Any]) -> str:
    lines = [
        "# 执行闸门基线：live vs off",
        "",
        "同一快照、同一确认口径（pending_mode=only）、同一退出（15 日 / -8% / +18%）、同一现金组合，"
        "只改 `--execution-regime-gate`。`off` 组回答「确认信号本身在被闸门拦掉的水温里赚不赚钱」。",
        "",
        f"- 判定规则：{report.get('decision_rule', '')}",
        "- 成本口径：`ret_pct` 已含买卖各 0.5% 摩擦；超额 = `ret_pct` − 同持有窗口的基准涨跌（上证 / 创业板指）。",
    ]
    if report.get("missing_cells"):
        lines.append(f"- 证据不完整，缺少：{', '.join(report['missing_cells'])}。")
    if report.get("ignored_dirs"):
        lines.append(f"- ⚠️ 不合 `backtest-gate-<period>-<gate>` 命名、已忽略：{', '.join(report['ignored_dirs'])}。")
    lines.extend(_cell_lines(report.get("cells", []), report.get("pooled") or {}))
    lines.extend(_verdict_lines(report.get("verdict") or {}))
    lines.extend(_marginal_lines(report.get("marginal") or {}))
    lines.extend(_regime_lines("仅 off 边际成交按水温（跨周期合并）", report.get("off_only_by_regime") or []))
    for gate in GATES:
        lines.extend(
            _regime_lines(f"{gate} 全部成交按水温（跨周期合并）", (report.get("pooled_by_regime") or {}).get(gate, []))
        )
    return "\n".join(lines) + "\n"


def _cell_lines(cells: list[dict[str, Any]], pooled: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## 分周期协议表",
        "",
        "| 周期 | 闸门 | 成交 | 均收 | 均收 95% CI | 胜率 | 超额·上证 | 超额·创业板 | 去前1/3/5 均收 | 现金收益 | 现金回撤 | 判定 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    lines.extend(_protocol_line(cell["period"], cell["gate"], cell) for cell in cells)
    lines.extend(
        _protocol_line("合并", gate, {**item, "cash_return": None, "cash_drawdown": None})
        for gate, item in pooled.items()
    )
    return lines


def _protocol_line(period: str, gate: str, item: dict[str, Any]) -> str:
    tail = item.get("tail") or {}
    return (
        f"| {period} | {gate} | {item.get('trades', 0)} | {fmt(item.get('avg_return'), '%')} | {_ci(item.get('ci95'))} | "
        f"{fmt(item.get('win_rate'), '%')} | {_excess_cell(item.get('excess_main'))} | {_excess_cell(item.get('excess_smallcap'))} | "
        f"{' / '.join(fmt(tail.get(f'drop_{drop}'), '%') for drop in TAIL_DROPS)} | "
        f"{fmt(item.get('cash_return'), '%')} | {fmt(item.get('cash_drawdown'), '%')} | {item.get('signal', '-')} |"
    )


def _excess_cell(excess: dict[str, Any] | None) -> str:
    excess = excess or {}
    return f"{fmt(excess.get('avg'), '%')} {_ci(excess.get('ci95'))}"


def _ci(ci: Any) -> str:
    return "-" if not ci else f"[{ci[0]:+.2f}, {ci[1]:+.2f}]"


def _verdict_lines(verdict: dict[str, Any]) -> list[str]:
    lines = ["", "## 跨周期判定", ""]
    for gate, item in verdict.items():
        lines.append(
            f"- {gate}：{item.get('periods', 0)} 个周期，正超额 {item.get('positive', 0)} / 负超额 {item.get('negative', 0)} / "
            f"不显著 {item.get('inconclusive', 0)} → {item.get('status', 'insufficient')}。"
        )
    return lines


def _marginal_lines(marginal: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## 边际成交（按 signal_date + code 配对）",
        "",
        "`仅 live` 是 live 有、off 没有的成交（同日被 off 组的其他候选挤掉）；`仅 off` 是闸门拦掉的成交。",
        "",
        "| 周期 | 共同 | 仅 live n / 均收 / 胜率 | 仅 off n / 均收 / 胜率 |",
        "|---|---:|---:|---:|",
    ]
    for period, item in marginal.items():
        lines.append(
            f"| {period} | {(item.get('common') or {}).get('trades', 0)} | "
            f"{_stats_cell(item.get('live_only'))} | {_stats_cell(item.get('off_only'))} |"
        )
    return lines


def _stats_cell(stats: dict[str, Any] | None) -> str:
    stats = stats or {}
    return f"{stats.get('trades', 0)} / {fmt(stats.get('avg_return'), '%')} / {fmt(stats.get('win_rate'), '%')}"


def _regime_lines(title: str, rows: list[dict[str, Any]]) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not rows:
        return [*lines, "- 无成交。"]
    lines.extend(["| 水温 | 成交 | 均收 | 胜率 | 超额·创业板 |", "|---|---:|---:|---:|---:|"])
    lines.extend(
        f"| {row['regime']} | {row['trades']} | {fmt(row.get('avg_return'), '%')} | "
        f"{fmt(row.get('win_rate'), '%')} | {fmt(row.get('excess_smallcap_avg'), '%')} |"
        for row in rows
    )
    return lines
