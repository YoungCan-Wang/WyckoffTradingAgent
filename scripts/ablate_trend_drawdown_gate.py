"""趋势回撤门槛（trend_cont_max_drawdown_pct）的因子消融，可复算。

PR #269 以「997 只样本、28,578 事件、T+5 差 +0.058pct、95% CI 跨 0」为据移除该硬门槛，
但仓库里没有可复算的脚本或数据，结论只存在于文档散文里。本脚本把该结论变成可重跑的
计算：对每个「趋势延续候选」事件，按事件日的 60 日最大回撤分成放行组（<20%）与拦截组
（>=20%），比较 T+1/T+5 的净收益、MFE、MAE。

关键方法约束（沿用本仓已有教训）：
- **按日等权**：先在每个交易日内对组内事件取均值，再对交易日求均值。否则事件多的
  日子会主导结果，等于用同一天的横截面噪声反复投票。
- **交易日聚类 bootstrap**：重采样单位是交易日而非单个事件，因为同一天的事件高度相关。
- **随机负控制**：同时报告「随机分组」在同样样本量下的差值分布宽度。若真实差值落在
  随机带宽内，则该门槛没有方向区分力——这是判定的核心依据，不是看差值符号。

用法::

    python scripts/ablate_trend_drawdown_gate.py --start 2026-02-01 --end 2026-08-01 \
        --max-symbols 400 --out artifacts/trend_drawdown_ablation
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import _bootstrap  # noqa: F401
import pandas as pd

GATE_PCT = 20.0
MIN_HISTORY = 220
TREND_DRAWDOWN_WINDOW = 60


def max_drawdown_pct(close: pd.Series, window: int = TREND_DRAWDOWN_WINDOW) -> float | None:
    """窗口内最大回撤（正数百分比）。

    与 core.layer2_strength._max_drawdown_pct 同口径，此处内联以便在合入 #269 之前
    也能独立跑数——脚本的结论正是用来判断 #269 该不该合。
    """
    recent = pd.to_numeric(close, errors="coerce").dropna().tail(max(int(window), 2))
    if len(recent) < 2:
        return None
    return abs(float((recent / recent.cummax() - 1.0).min()) * 100.0)


@dataclass
class Event:
    code: str
    date: str
    drawdown: float
    ret1: float
    ret5: float
    mfe5: float
    mae5: float


@dataclass
class GroupStats:
    events: int = 0
    days: int = 0
    ret1: float | None = None
    ret5: float | None = None
    mfe5: float | None = None
    mae5: float | None = None
    per_day_ret5: list[float] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="趋势回撤门槛因子消融（可复算）")
    parser.add_argument("--start", required=True, help="事件区间起始 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="事件区间结束 YYYY-MM-DD")
    parser.add_argument("--max-symbols", type=int, default=400, help="抽样标的数上限")
    parser.add_argument("--bootstrap", type=int, default=2000, help="交易日聚类 bootstrap 次数")
    parser.add_argument("--seeds", type=int, default=8, help="随机负控制的种子数")
    parser.add_argument("--out", default="artifacts/trend_drawdown_ablation", help="输出目录")
    return parser.parse_args()


def _forward_metrics(frame: pd.DataFrame, idx: int) -> tuple[float, float, float, float] | None:
    """事件次日开盘视为建仓，返回 (T+1, T+5, MFE5, MAE5)，单位百分数。"""
    if idx + 6 >= len(frame):
        return None
    entry = float(frame["close"].iloc[idx])
    if entry <= 0:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame.get("high", close), errors="coerce")
    low = pd.to_numeric(frame.get("low", close), errors="coerce")
    window_high = high.iloc[idx + 1 : idx + 6]
    window_low = low.iloc[idx + 1 : idx + 6]
    if window_high.isna().all() or window_low.isna().all():
        return None
    ret1 = (float(close.iloc[idx + 1]) / entry - 1.0) * 100.0
    ret5 = (float(close.iloc[idx + 5]) / entry - 1.0) * 100.0
    mfe5 = (float(window_high.max()) / entry - 1.0) * 100.0
    mae5 = (float(window_low.min()) / entry - 1.0) * 100.0
    return ret1, ret5, mfe5, mae5


def _day_weighted(values: dict[str, list[float]]) -> float | None:
    """按交易日等权：先日内均值，再跨日均值。"""
    per_day = [mean(items) for items in values.values() if items]
    return mean(per_day) if per_day else None


def _group_stats(events: list[Event]) -> GroupStats:
    if not events:
        return GroupStats()
    by_day: dict[str, dict[str, list[float]]] = {}
    for event in events:
        bucket = by_day.setdefault(event.date, {"ret1": [], "ret5": [], "mfe5": [], "mae5": []})
        bucket["ret1"].append(event.ret1)
        bucket["ret5"].append(event.ret5)
        bucket["mfe5"].append(event.mfe5)
        bucket["mae5"].append(event.mae5)
    return GroupStats(
        events=len(events),
        days=len(by_day),
        ret1=_day_weighted({day: vals["ret1"] for day, vals in by_day.items()}),
        ret5=_day_weighted({day: vals["ret5"] for day, vals in by_day.items()}),
        mfe5=_day_weighted({day: vals["mfe5"] for day, vals in by_day.items()}),
        mae5=_day_weighted({day: vals["mae5"] for day, vals in by_day.items()}),
        per_day_ret5=[mean(vals["ret5"]) for vals in by_day.values() if vals["ret5"]],
    )


def _cluster_bootstrap_ci(
    passed: list[Event], blocked: list[Event], rounds: int, rng: random.Random
) -> tuple[float, float]:
    """按交易日重采样，返回 (放行组 - 拦截组) T+5 差值的 95% CI。"""
    days = sorted({event.date for event in passed} | {event.date for event in blocked})
    if len(days) < 3:
        return (float("nan"), float("nan"))
    by_day_pass: dict[str, list[float]] = {}
    by_day_block: dict[str, list[float]] = {}
    for event in passed:
        by_day_pass.setdefault(event.date, []).append(event.ret5)
    for event in blocked:
        by_day_block.setdefault(event.date, []).append(event.ret5)

    diffs: list[float] = []
    for _ in range(rounds):
        sample = [days[rng.randrange(len(days))] for _ in days]
        left = [mean(by_day_pass[day]) for day in sample if by_day_pass.get(day)]
        right = [mean(by_day_block[day]) for day in sample if by_day_block.get(day)]
        if left and right:
            diffs.append(mean(left) - mean(right))
    if not diffs:
        return (float("nan"), float("nan"))
    diffs.sort()
    lo = diffs[int(0.025 * (len(diffs) - 1))]
    hi = diffs[int(0.975 * (len(diffs) - 1))]
    return (lo, hi)


def _random_control_band(events: list[Event], n_passed: int, seeds: int) -> tuple[float, float]:
    """随机分组负控制：同样本量下差值的最小/最大值，作为噪声带宽。"""
    diffs: list[float] = []
    for seed in range(seeds):
        rng = random.Random(seed)
        shuffled = events[:]
        rng.shuffle(shuffled)
        left = _group_stats(shuffled[:n_passed]).ret5
        right = _group_stats(shuffled[n_passed:]).ret5
        if left is not None and right is not None:
            diffs.append(left - right)
    return (min(diffs), max(diffs)) if diffs else (float("nan"), float("nan"))


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    events = collect_events(args)
    if not events:
        print("[ablation] 没有采集到事件，检查区间与样本量")
        return 1
    report = build_report(events, args)
    (out_dir / "trend_drawdown_ablation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pd.DataFrame([vars(event) for event in events]).to_csv(out_dir / "events.csv", index=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def collect_events(args: argparse.Namespace) -> list[Event]:
    """遍历标的，对每个满足趋势延续前置条件的交易日产出一个事件。"""
    from core.layer2_strength import close_return_pct
    from integrations.fetch_a_share_csv import get_stocks_by_board
    from workflows.backtest_data import fetch_online_history_map

    codes = [str(item.get("code", "")).strip() for item in get_stocks_by_board("all")]
    codes = [code for code in codes if code][: max(int(args.max_symbols), 1)]
    print(f"[ablation] 标的池 {len(codes)}，区间 {args.start} ~ {args.end}")
    # 事件日要算 60 日回撤与 MA200，需要区间前至少 220 个交易日的历史。
    fetch_start = (pd.to_datetime(args.start) - pd.Timedelta(days=560)).date()
    hist_map, failures = fetch_online_history_map(codes, fetch_start, pd.to_datetime(args.end).date(), max_workers=8)
    print(f"[ablation] 历史可用 {len(hist_map)}/{len(codes)}（失败 {len(failures)}）")

    start = pd.to_datetime(args.start).date()
    end = pd.to_datetime(args.end).date()
    events: list[Event] = []
    for code, frame in hist_map.items():
        if frame is None or len(frame) < MIN_HISTORY:
            continue
        work = frame.reset_index(drop=True)
        dates = pd.to_datetime(work["date"], errors="coerce").dt.date
        close = pd.to_numeric(work["close"], errors="coerce")
        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()
        for idx in range(MIN_HISTORY, len(work)):
            day = dates.iloc[idx]
            if day is None or not (start <= day <= end):
                continue
            # 趋势延续通道的其余前置条件保持不变，只切换回撤门槛。
            if not (close.iloc[idx] > ma50.iloc[idx] > ma200.iloc[idx]):
                continue
            ret20 = close_return_pct(close.iloc[: idx + 1], 20)
            if ret20 is None or ret20 <= 0:
                continue
            drawdown = max_drawdown_pct(close.iloc[: idx + 1], TREND_DRAWDOWN_WINDOW)
            forward = _forward_metrics(work, idx)
            if drawdown is None or forward is None:
                continue
            events.append(Event(code, day.isoformat(), drawdown, *forward))
    print(f"[ablation] 事件数 {len(events)}，标的 {len({e.code for e in events})}")
    return events


def build_report(events: list[Event], args: argparse.Namespace) -> dict:
    passed = [event for event in events if event.drawdown < GATE_PCT]
    blocked = [event for event in events if event.drawdown >= GATE_PCT]
    stats_pass = _group_stats(passed)
    stats_block = _group_stats(blocked)
    rng = random.Random(0)
    ci = _cluster_bootstrap_ci(passed, blocked, int(args.bootstrap), rng)
    band = _random_control_band(events, len(passed), int(args.seeds))

    def _diff(left: float | None, right: float | None) -> float | None:
        return None if left is None or right is None else round(left - right, 4)

    ci_crosses_zero = not (ci[0] > 0 or ci[1] < 0) if ci[0] == ci[0] else None
    real_diff = _diff(stats_pass.ret5, stats_block.ret5)
    inside_band = None
    if real_diff is not None and band[0] == band[0]:
        inside_band = band[0] <= real_diff <= band[1]
    return {
        "window": {"start": args.start, "end": args.end},
        "gate_pct": GATE_PCT,
        "events_total": len(events),
        "symbols": len({event.code for event in events}),
        "passed": {k: v for k, v in vars(stats_pass).items() if k != "per_day_ret5"},
        "blocked": {k: v for k, v in vars(stats_block).items() if k != "per_day_ret5"},
        "diff_passed_minus_blocked": {
            "ret1_pct": _diff(stats_pass.ret1, stats_block.ret1),
            "ret5_pct": real_diff,
            "mfe5_pct": _diff(stats_pass.mfe5, stats_block.mfe5),
            "mae5_pct": _diff(stats_pass.mae5, stats_block.mae5),
        },
        "ret5_cluster_bootstrap_95ci": [
            None if ci[0] != ci[0] else round(ci[0], 4),
            None if ci[1] != ci[1] else round(ci[1], 4),
        ],
        "ret5_random_control_band": [
            None if band[0] != band[0] else round(band[0], 4),
            None if band[1] != band[1] else round(band[1], 4),
        ],
        "verdict": {
            "ci_crosses_zero": ci_crosses_zero,
            "real_diff_inside_random_band": inside_band,
            "reading": (
                "CI 跨 0 且真实差值落在随机带宽内 → 该门槛无方向区分力"
                if ci_crosses_zero and inside_band
                else "存在超出噪声的差异，需进一步核查"
            ),
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
