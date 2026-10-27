"""水温 / 情绪周期作为纯择时信号、不选股只持有指数时的净值评估（研究对照，不改生产）。

输入来自闸门基线分片：每日水温取 ``selection_coverage_*.json``（六片同周期的水温序列完全相同，
取第一片即可），情绪计数取 ``market_sentiment_*.csv``，指数行情取分片随产物一并上传的
``benchmark_main.csv`` / ``benchmark_smallcap.csv``。
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
VARIANTS = (
    "buy_hold",
    "avoid_hard_defense",
    "risk_on_only",
    "production_gate",
    "sentiment_follow",
    "sentiment_not_ice",
    "sentiment_ice_contrarian",
)
LABEL_KEYS = (("regime", "水温"), ("sentiment", "情绪状态（净涨停数 250 日分位）"))
# 情绪状态：全市场净涨停数（涨停 − 跌停）在过去 250 个交易日的分位；窗口与 20/50/80 阈值预登记，不调。
SENTIMENT_WINDOW = 250
SENTIMENT_MIN_OBS = 120
# ETF 无印花税，佣金按万一；滑点沿用 core.trade_friction 的每边默认值。
ETF_COMMISSION_PCT_PER_SIDE = 0.01
MIN_PERIODS = 4
DECISION_RULE = (
    "状态在 T 日收盘判定，T+1 开盘切换仓位，持仓期按收盘价计净值，每次切换扣 ETF 单边佣金与滑点；"
    "所有变体与持有基准都只在水温与情绪状态都可得的同一组交易日上计算。"
    "判定只看两件事：六个基线周期里变体扣费后收益高于持有基准的周期数，以及合并全部周期后"
    "「在场日」与「离场日」次日指数收益之差的 95% bootstrap CI。两个基准都满足周期数 ≥ 4 且 CI > 0 才记"
    "`timing_value`；只在回撤上 ≥ 4 个周期更小且 CI 不为负记 `drawdown_only`；其余记 `no_timing_value`。"
    "单个周期的现金收益不构成结论——防守型变体在熊市周期必然领先，在牛市周期必然落后。"
    "情绪状态取全市场净涨停数在过去 250 日的分位：<20% 冰点 ice、20–50% 退潮 ebb、50–80% 回暖 warm、"
    "≥80% 高潮 climax；sentiment_follow 在 warm/climax 在场，sentiment_not_ice 只避开 ice，"
    "sentiment_ice_contrarian 只在 ice 在场。"
)


@dataclass(frozen=True)
class BenchBar:
    date: str
    open: float
    close: float


@dataclass(frozen=True)
class RegimeTimingInput:
    period: str
    labels: dict[str, dict[str, str]]
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
        labels = _merge_labels(_load_regimes(path), _load_sentiment(path.parent))
        if "main" in bench and labels:
            inputs[match.group("period")] = RegimeTimingInput(match.group("period"), labels, bench)
    return inputs


def _merge_labels(regimes: dict[str, str], sentiment: dict[str, str]) -> dict[str, dict[str, str]]:
    """只保留两种标签都有的交易日，保证所有变体在同一组日子上比较。"""
    return {day: {"regime": regimes[day], "sentiment": sentiment[day]} for day in regimes if day in sentiment}


def _load_regimes(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item["signal_date"])[:10]: normalize_regime(item.get("regime"))
        for item in data.get("selection_coverage", [])
        if item.get("signal_date")
    }


def _load_sentiment(directory: Path) -> dict[str, str]:
    paths = sorted(directory.glob("market_sentiment_*.csv"))
    if not paths:
        return {}
    with paths[0].open(encoding="utf-8-sig", newline="") as handle:
        rows = sorted(
            (_iso_date(row["date"]), int(row["up_limit"]) - int(row["down_limit"])) for row in csv.DictReader(handle)
        )
    return sentiment_states([day for day, _ in rows], [value for _, value in rows])


def sentiment_states(days: list[str], net_limits: list[int]) -> dict[str, str]:
    out: dict[str, str] = {}
    for idx, (day, value) in enumerate(zip(days, net_limits, strict=True)):
        window = net_limits[max(0, idx + 1 - SENTIMENT_WINDOW) : idx + 1]
        if len(window) < SENTIMENT_MIN_OBS:
            continue
        pct = sum(item < value for item in window) / len(window)
        out[day] = "ice" if pct < 0.2 else "ebb" if pct < 0.5 else "warm" if pct < 0.8 else "climax"
    return out


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


def in_market(variant: str, label: dict[str, str], blocked: frozenset[str]) -> bool:
    regime, sentiment = label["regime"], label["sentiment"]
    if variant == "buy_hold":
        return True
    if variant == "avoid_hard_defense":
        return regime not in NO_NEW_BUY_REGIMES
    if variant == "risk_on_only":
        return regime == "RISK_ON"
    if variant == "production_gate":
        return regime not in blocked
    if variant == "sentiment_follow":
        return sentiment in {"warm", "climax"}
    if variant == "sentiment_not_ice":
        return sentiment != "ice"
    return sentiment == "ice"


def simulate_timing(
    bars: tuple[BenchBar, ...], labels: dict[str, dict[str, str]], variant: str, blocked: frozenset[str]
) -> dict:
    side_cost = (ETF_COMMISSION_PCT_PER_SIDE + slippage_bps_per_side() / 100.0) / 100.0
    equity = peak = 1.0
    drawdown = 0.0
    prev_state = days = in_days = switches = 0
    for prev, cur in zip(bars, bars[1:], strict=False):
        label = labels.get(prev.date)
        if label is None:
            continue
        state = int(in_market(variant, label, blocked))
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


LabeledReturn = tuple[dict[str, str], float]


def _forward_returns(bars: tuple[BenchBar, ...], labels: dict[str, dict[str, str]]) -> list[LabeledReturn]:
    out: list[LabeledReturn] = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        label = labels.get(prev.date)
        if label is not None:
            out.append((label, (cur.close / prev.close - 1.0) * 100.0))
    return out


def build_regime_timing(inputs: dict[str, RegimeTimingInput]) -> dict[str, Any]:
    blocked = oms_buy_block_regimes()
    cells = [
        {
            "period": period,
            "benchmark": name,
            "variant": variant,
            **simulate_timing(bars, item.labels, variant, blocked),
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
            for pair in _forward_returns(item.bench[name], item.labels)
        ]
        for name, _ in BENCHMARKS
    }
    conditional = {
        key: {name: _by_label(pairs, key) for name, pairs in forward.items() if pairs} for key, _ in LABEL_KEYS
    }
    verdict = {variant: _verdict(variant, cells, forward, blocked) for variant in VARIANTS if variant != "buy_hold"}
    missing = [period for period in BASELINE_PERIODS if period not in inputs]
    regimes = {label["regime"] for item in inputs.values() for label in item.labels.values()}
    return {
        "status": "ready" if not missing else "incomplete",
        "missing_periods": missing,
        "production_gate_allowed": sorted(regime for regime in regimes if regime not in blocked),
        "decision_rule": DECISION_RULE,
        "cells": cells,
        "conditional_forward": conditional,
        "verdict": verdict,
    }


def _by_label(pairs: list[LabeledReturn], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[float]] = {}
    for label, ret in pairs:
        groups.setdefault(label[key], []).append(ret)
    rows = [
        {
            "label": state,
            "days": len(values),
            "avg_next_day": mean(values),
            "ci95": bootstrap_mean_ci(values),
            "hit_rate": sum(value > 0 for value in values) / len(values) * 100.0,
        }
        for state, values in groups.items()
    ]
    return sorted(rows, key=lambda row: -row["days"])


def _verdict(variant: str, cells: list[dict], forward: dict[str, list[LabeledReturn]], blocked: frozenset[str]) -> dict:
    detail: dict[str, Any] = {}
    for name, _ in BENCHMARKS:
        periods = _baseline_pairs(cells, name, variant)
        detail[name] = {
            "periods": len(periods),
            "return_wins": sum(item["total_return"] > base["total_return"] for item, base in periods),
            "drawdown_wins": sum(item["max_drawdown"] > base["max_drawdown"] for item, base in periods),
            "in_out_diff": _in_out_diff(forward.get(name, []), variant, blocked),
        }
    return {"status": _verdict_status(detail), **detail}


def _baseline_pairs(cells: list[dict], benchmark: str, variant: str) -> list[tuple[dict, dict]]:
    base = {c["period"]: c for c in cells if c["benchmark"] == benchmark and c["variant"] == "buy_hold" and c["days"]}
    return [
        (cell, base[cell["period"]])
        for cell in cells
        if cell["benchmark"] == benchmark and cell["variant"] == variant and cell["days"]
        if cell["period"] in base and cell["period"] in BASELINE_PERIODS
    ]


def _in_out_diff(pairs: list[LabeledReturn], variant: str, blocked: frozenset[str]) -> dict[str, Any]:
    inside = [ret for label, ret in pairs if in_market(variant, label, blocked)]
    outside = [ret for label, ret in pairs if not in_market(variant, label, blocked)]
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
        "# 水温 / 情绪周期纯择时基线：不选股、只按状态持有/空仓指数",
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
    for key, title in LABEL_KEYS:
        lines += ["", f"## {title} → 次日指数收益（跨周期合并，不扣费）", ""]
        for name, label in BENCHMARKS:
            rows = report["conditional_forward"].get(key, {}).get(name)
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
    lines = [f"### {label}", "", "| 状态 | 天数 | 次日均收 | CI95 | 上涨占比 |", "|---|---:|---:|---|---:|"]
    lines += [
        f"| {row['label']} | {row['days']} | {fmt(row['avg_next_day'], '%')} | {_ci(row['ci95'])} | {_pct(row['hit_rate'])} |"
        for row in rows
    ]
    return lines + [""]


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}%"


def _ci(ci: Any) -> str:
    return "-" if not ci else f"[{ci[0]:+.2f}, {ci[1]:+.2f}]"
