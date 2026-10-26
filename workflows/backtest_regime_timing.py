"""水温作为纯择时信号、不选股只持有指数时的净值评估（研究对照，不改生产）。

输入来自闸门基线分片：每日水温取 ``selection_coverage_*.json``（六片同周期的水温序列完全相同，
取第一片即可），指数行情取分片随产物一并上传的 ``benchmark_main.csv`` / ``benchmark_smallcap.csv``。
"""

from __future__ import annotations

import csv
import glob
import json
import random
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from core.market_trade_mode import NO_NEW_BUY_REGIMES, normalize_regime, oms_buy_block_regimes
from core.trade_friction import slippage_bps_per_side
from workflows.backtest_gate_baseline import BASELINE_PERIODS, GATE_DIR_PATTERN
from workflows.backtest_report_stats import BOOTSTRAP_RESAMPLES, bootstrap_mean_ci, fmt

BENCHMARKS = (("main", "上证"), ("smallcap", "创业板"))
VARIANTS = ("buy_hold", "avoid_hard_defense", "risk_on_only", "production_gate")
# ETF 无印花税，佣金按万一；滑点沿用 core.trade_friction 的每边默认值。
ETF_COMMISSION_PCT_PER_SIDE = 0.01
MIN_PERIODS = 4
DECISION_RULE = (
    "水温在 T 日收盘判定，T+1 开盘切换仓位，持仓期按收盘价计净值，每次切换扣 ETF 单边佣金与滑点；"
    "所有变体与持有基准都只在水温可得的同一组交易日上计算。"
    "判定只看两件事：六个基线周期里变体扣费后收益高于持有基准的周期数，以及合并全部周期后"
    "「在场日」与「离场日」次日指数收益之差的 95% bootstrap CI。两个基准都满足周期数 ≥ 4 且 CI > 0 才记"
    "`timing_value`；只在回撤上 ≥ 4 个周期更小且 CI 不为负记 `drawdown_only`；其余记 `no_timing_value`。"
    "单个周期的现金收益不构成结论——防守型变体在熊市周期必然领先，在牛市周期必然落后。"
)


@dataclass(frozen=True)
class BenchBar:
    date: str
    open: float
    close: float


@dataclass(frozen=True)
class RegimeTimingInput:
    period: str
    regimes: dict[str, str]
    bench: dict[str, tuple[BenchBar, ...]]


def load_regime_timing_inputs(artifacts_dir: Path) -> dict[str, RegimeTimingInput]:
    inputs: dict[str, RegimeTimingInput] = {}
    paths = sorted(Path(p) for p in glob.glob(str(artifacts_dir / "**" / "selection_coverage_*.json"), recursive=True))
    for path in paths:
        match = GATE_DIR_PATTERN.search(path.parent.name)
        if not match or match.group("period") in inputs:
            continue
        bench = {name: _load_bench(path.parent / f"benchmark_{name}.csv") for name, _ in BENCHMARKS}
        bench = {name: bars for name, bars in bench.items() if bars}
        regimes = _load_regimes(path)
        if "main" in bench and regimes:
            inputs[match.group("period")] = RegimeTimingInput(match.group("period"), regimes, bench)
    return inputs


def _load_regimes(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item["signal_date"])[:10]: normalize_regime(item.get("regime"))
        for item in data.get("selection_coverage", [])
        if item.get("signal_date")
    }


def _load_bench(path: Path) -> tuple[BenchBar, ...]:
    if not path.exists():
        return ()
    bars: list[BenchBar] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                bars.append(BenchBar(_iso_date(row["date"]), float(row["open"]), float(row["close"])))
            except (KeyError, TypeError, ValueError):
                continue
    return tuple(sorted(bars, key=lambda bar: bar.date))


def _iso_date(raw: str) -> str:
    text = str(raw).strip()[:10]
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def in_market(variant: str, regime: str, blocked: frozenset[str]) -> bool:
    if variant == "buy_hold":
        return True
    if variant == "avoid_hard_defense":
        return regime not in NO_NEW_BUY_REGIMES
    if variant == "risk_on_only":
        return regime == "RISK_ON"
    return regime not in blocked


def simulate_timing(bars: tuple[BenchBar, ...], regimes: dict[str, str], variant: str, blocked: frozenset[str]) -> dict:
    side_cost = (ETF_COMMISSION_PCT_PER_SIDE + slippage_bps_per_side() / 100.0) / 100.0
    equity = peak = 1.0
    drawdown = 0.0
    prev_state = days = in_days = switches = 0
    for prev, cur in zip(bars, bars[1:], strict=False):
        regime = regimes.get(prev.date)
        if regime is None:
            continue
        state = int(in_market(variant, regime, blocked))
        if state and prev_state:
            ret = cur.close / prev.close - 1.0
        elif state:
            ret, switches = cur.close / cur.open - 1.0 - side_cost, switches + 1
        elif prev_state:
            ret, switches = cur.open / prev.close - 1.0 - side_cost, switches + 1
        else:
            ret = 0.0
        days, in_days, prev_state = days + 1, in_days + state, state
        equity *= 1.0 + ret
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return {
        "days": days,
        "total_return": (equity - 1.0) * 100.0 if days else None,
        "max_drawdown": drawdown * 100.0 if days else None,
        "time_in_market": in_days / days * 100.0 if days else None,
        "switches": switches,
    }


def _forward_returns(bars: tuple[BenchBar, ...], regimes: dict[str, str]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        regime = regimes.get(prev.date)
        if regime is not None:
            out.append((regime, (cur.close / prev.close - 1.0) * 100.0))
    return out


def build_regime_timing(inputs: dict[str, RegimeTimingInput]) -> dict[str, Any]:
    blocked = oms_buy_block_regimes()
    cells = [
        {
            "period": period,
            "benchmark": name,
            "variant": variant,
            **simulate_timing(bars, item.regimes, variant, blocked),
        }
        for period, item in sorted(inputs.items())
        for name, bars in item.bench.items()
        for variant in VARIANTS
    ]
    forward = {
        name: [
            pair
            for item in inputs.values()
            if name in item.bench
            for pair in _forward_returns(item.bench[name], item.regimes)
        ]
        for name, _ in BENCHMARKS
    }
    conditional = {name: _by_regime(pairs) for name, pairs in forward.items() if pairs}
    verdict = {variant: _verdict(variant, cells, forward, blocked) for variant in VARIANTS if variant != "buy_hold"}
    missing = [period for period in BASELINE_PERIODS if period not in inputs]
    return {
        "status": "ready" if not missing else "incomplete",
        "missing_periods": missing,
        "production_gate_allowed": sorted(
            regime for regime in _known_regimes(inputs) if in_market("production_gate", regime, blocked)
        ),
        "decision_rule": DECISION_RULE,
        "cells": cells,
        "conditional_forward": conditional,
        "verdict": verdict,
    }


def _known_regimes(inputs: dict[str, RegimeTimingInput]) -> set[str]:
    return {regime for item in inputs.values() for regime in item.regimes.values()}


def _by_regime(pairs: list[tuple[str, float]]) -> list[dict[str, Any]]:
    groups: dict[str, list[float]] = {}
    for regime, ret in pairs:
        groups.setdefault(regime, []).append(ret)
    rows = []
    for regime, values in groups.items():
        rows.append(
            {
                "regime": regime,
                "days": len(values),
                "avg_next_day": mean(values),
                "ci95": bootstrap_mean_ci(values),
                "hit_rate": sum(value > 0 for value in values) / len(values) * 100.0,
            }
        )
    return sorted(rows, key=lambda row: -row["days"])


def _verdict(
    variant: str, cells: list[dict], forward: dict[str, list[tuple[str, float]]], blocked: frozenset[str]
) -> dict:
    detail: dict[str, Any] = {}
    for name, _ in BENCHMARKS:
        periods = _baseline_pairs(cells, name, variant)
        in_out = _in_out_diff(forward.get(name, []), variant, blocked)
        detail[name] = {
            "periods": len(periods),
            "return_wins": sum(item["total_return"] > base["total_return"] for item, base in periods),
            "drawdown_wins": sum(item["max_drawdown"] > base["max_drawdown"] for item, base in periods),
            "in_out_diff": in_out,
        }
    status = _verdict_status(detail)
    return {"status": status, **detail}


def _baseline_pairs(cells: list[dict], benchmark: str, variant: str) -> list[tuple[dict, dict]]:
    base = {c["period"]: c for c in cells if c["benchmark"] == benchmark and c["variant"] == "buy_hold" and c["days"]}
    return [
        (cell, base[cell["period"]])
        for cell in cells
        if cell["benchmark"] == benchmark and cell["variant"] == variant and cell["days"]
        if cell["period"] in base and cell["period"] in BASELINE_PERIODS
    ]


def _in_out_diff(pairs: list[tuple[str, float]], variant: str, blocked: frozenset[str]) -> dict[str, Any]:
    inside = [ret for regime, ret in pairs if in_market(variant, regime, blocked)]
    outside = [ret for regime, ret in pairs if not in_market(variant, regime, blocked)]
    if len(inside) < 2 or len(outside) < 2:
        return {"in_days": len(inside), "out_days": len(outside), "diff": None, "ci95": None}
    rng = random.Random(0)
    diffs = sorted(
        mean(rng.choices(inside, k=len(inside))) - mean(rng.choices(outside, k=len(outside)))
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    ci = (diffs[int(BOOTSTRAP_RESAMPLES * 0.025)], diffs[int(BOOTSTRAP_RESAMPLES * 0.975) - 1])
    return {"in_days": len(inside), "out_days": len(outside), "diff": mean(inside) - mean(outside), "ci95": ci}


def _verdict_status(detail: dict[str, dict]) -> str:
    if any(item["periods"] < MIN_PERIODS or item["in_out_diff"]["ci95"] is None for item in detail.values()):
        return "insufficient"
    if all(item["return_wins"] >= MIN_PERIODS and item["in_out_diff"]["ci95"][0] > 0 for item in detail.values()):
        return "timing_value"
    if all(item["drawdown_wins"] >= MIN_PERIODS and item["in_out_diff"]["ci95"][1] >= 0 for item in detail.values()):
        return "drawdown_only"
    return "no_timing_value"


def render_regime_timing(report: dict[str, Any]) -> str:
    lines = [
        "# 水温纯择时基线：不选股、只按水温持有/空仓指数",
        "",
        report["decision_rule"],
        "",
        f"- 状态: `{report['status']}`"
        + (f"（缺 {', '.join(report['missing_periods'])}）" if report["missing_periods"] else ""),
        f"- `production_gate` 在场水温（读自 STEP4_BUY_BLOCK/ALLOW）: {', '.join(report['production_gate_allowed']) or '-'}",
        f"- `avoid_hard_defense` 离场水温: {', '.join(sorted(NO_NEW_BUY_REGIMES))}",
        "",
        "## 判定",
        "",
        "| 变体 | 判定 | 基准 | 周期 | 收益胜 | 回撤胜 | 在场−离场 次日均收 | CI95 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for variant, verdict in report["verdict"].items():
        for name, label in BENCHMARKS:
            item = verdict[name]
            diff = item["in_out_diff"]
            lines.append(
                f"| {variant} | `{verdict['status']}` | {label} | {item['periods']} | {item['return_wins']} | "
                f"{item['drawdown_wins']} | {fmt(diff['diff'], '%')} ({diff['in_days']}/{diff['out_days']}) | {_ci(diff['ci95'])} |"
            )
    lines += ["", "## 分周期净值（扣费）", ""]
    lines += _cell_lines(report["cells"])
    lines += ["", "## 水温 → 次日指数收益（跨周期合并，不扣费）", ""]
    for name, label in BENCHMARKS:
        rows = report["conditional_forward"].get(name)
        if rows:
            lines += _conditional_lines(label, rows)
    return "\n".join(lines) + "\n"


def _cell_lines(cells: list[dict]) -> list[str]:
    lines = [
        "| 周期 | 基准 | 变体 | 收益 | vs 持有 | 最大回撤 | 在场 | 切换 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    base = {(c["period"], c["benchmark"]): c["total_return"] for c in cells if c["variant"] == "buy_hold"}
    labels = dict(BENCHMARKS)
    for cell in cells:
        hold = base.get((cell["period"], cell["benchmark"]))
        excess = cell["total_return"] - hold if cell["total_return"] is not None and hold is not None else None
        lines.append(
            f"| {cell['period']} | {labels.get(cell['benchmark'], cell['benchmark'])} | {cell['variant']} | "
            f"{fmt(cell['total_return'], '%')} | {fmt(excess, 'pp')} | {fmt(cell['max_drawdown'], '%')} | "
            f"{_pct(cell['time_in_market'])} | {cell['switches']} |"
        )
    return lines


def _conditional_lines(label: str, rows: list[dict]) -> list[str]:
    lines = [f"### {label}", "", "| 水温 | 天数 | 次日均收 | CI95 | 上涨占比 |", "|---|---:|---:|---|---:|"]
    lines += [
        f"| {row['regime']} | {row['days']} | {fmt(row['avg_next_day'], '%')} | {_ci(row['ci95'])} | {_pct(row['hit_rate'])} |"
        for row in rows
    ]
    return lines + [""]


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}%"


def _ci(ci: Any) -> str:
    return "-" if not ci else f"[{ci[0]:+.2f}, {ci[1]:+.2f}]"
