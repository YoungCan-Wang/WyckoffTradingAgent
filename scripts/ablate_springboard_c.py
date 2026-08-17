"""起跳板条件 C（支撑位触碰）的容差消融，可复算。

背景与动机：
- 生产实测 C 的命中率为 4,509/4,895 = 92%，等于恒真标签；且同日配对贡献为 −3.02pct、
  仅 38% 的交易日为正。而 B（放量突破）是唯一正贡献条件（+2.35pct、59% 为正），
  「仅 B」是唯一均值为正的组合（+0.93%）。
- 根因是容差按价格水平算：``tol = support * 0.05``。实测低波动票（平安银行、茅台）的
  容差带盖住 60 日区间的 65–70%，任何低点都算「触碰支撑」，触碰数达 14–15 次，
  而门槛只要 >= 2。容差带占振幅的中位数为 38.3%。
- 直接摘掉 C 不可行：met_count 上限会从 3 变 2，``weak_confirmation_min_abc>=2``
  的通过率从 43.8% 塌到 3.2%，``pure_sos_min_abc>=3`` 变成永远不可满足的死条件。
  所以方向是修容差精度，保持 0–3 的分值域不变。

本脚本扫 ATR 归一化容差 ``tol = atr14 * k``，与现状（固定 5%）对照。

**必须按 signal_type 分层**：``compute_support_level`` 对不同信号用不同定义，其中
``sos`` 取的是「21 日最高价」——那是阻力位而非支撑位，用它算「低点触碰支撑」在语义上
就不成立。混在一起调 k 会得出没有意义的最优值。

方法约束沿用本仓既有教训：按交易日等权、同日配对、随机负控制在日内打乱标签、
前后段样本外切分。任何一档 k 若样本不足或落在噪声带内，明确标注而不给结论。

用法::

    python scripts/ablate_springboard_c.py --start 2026-05-25 --end 2026-08-07 \
        --max-symbols 300 --out docs/evidence
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from statistics import mean
from typing import Any

import _bootstrap  # noqa: F401
import pandas as pd

K_GRID = (0.3, 0.5, 0.75, 1.0, 1.5)
MIN_GROUP = 30
CONTROL_SEEDS = 20
ATR_WINDOW = 14
SUPPORT_WINDOW = 60
TOUCH_MIN = 2
# sos 的 support 是 21 日最高价（阻力位），语义上不适用「低点触碰支撑」。
RESISTANCE_BASED_SIGNALS = frozenset({"sos"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="起跳板 C 的容差消融")
    parser.add_argument("--start", default="2026-05-25", help="事件区间起始")
    parser.add_argument("--end", default="2026-08-07", help="事件区间结束")
    parser.add_argument("--max-symbols", type=int, default=300, help="抽样标的上限")
    parser.add_argument("--out", default="docs/evidence", help="输出目录")
    return parser.parse_args()


def atr(frame: pd.DataFrame, window: int = ATR_WINDOW) -> pd.Series:
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    prev = close.shift(1)
    span = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return span.rolling(window).mean()


def count_touches(low: pd.Series, support: float, tolerance: float, window: int = SUPPORT_WINDOW) -> int:
    if support <= 0 or tolerance <= 0:
        return 0
    recent = pd.to_numeric(low, errors="coerce").dropna().tail(window)
    return int(((recent - support).abs() <= tolerance).sum())


def _day_weighted(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    return float(frame.groupby("date").ret.mean().mean())


def _paired_diff(rows: list[dict[str, Any]], flag: str) -> tuple[float | None, int, float | None]:
    """同日配对差：剥离市场水温。返回 (均值, 配对交易日数, 为正比例)。"""
    if not rows:
        return (None, 0, None)
    frame = pd.DataFrame(rows)
    diffs = []
    for _, group in frame.groupby("date"):
        hit = group[group[flag]]
        miss = group[~group[flag]]
        if not hit.empty and not miss.empty:
            diffs.append(hit.ret.mean() - miss.ret.mean())
    if not diffs:
        return (None, 0, None)
    return (mean(diffs), len(diffs), 100.0 * sum(1 for d in diffs if d > 0) / len(diffs))


def _noise_band(rows: list[dict[str, Any]], flag: str) -> tuple[float | None, float | None]:
    """在每个交易日内打乱标签，保留聚类结构后的噪声带宽。"""
    if not rows:
        return (None, None)
    frame = pd.DataFrame(rows)
    diffs = []
    for seed in range(CONTROL_SEEDS):
        rng = random.Random(seed)
        parts = []
        for _, group in frame.groupby("date"):
            block = group.copy()
            hits = int(block[flag].sum())
            index = list(block.index)
            rng.shuffle(index)
            picked = set(index[:hits])
            block["_fake"] = [idx in picked for idx in block.index]
            parts.append(block)
        shuffled = pd.concat(parts)
        left = shuffled[shuffled._fake]
        right = shuffled[~shuffled._fake]
        if not left.empty and not right.empty:
            diffs.append(left.groupby("date").ret.mean().mean() - right.groupby("date").ret.mean().mean())
    if not diffs:
        return (None, None)
    return (round(min(diffs), 4), round(max(diffs), 4))


def evaluate_variant(rows: list[dict[str, Any]], flag: str, label: str) -> dict[str, Any]:
    hit = [r for r in rows if r[flag]]
    miss = [r for r in rows if not r[flag]]
    if len(hit) < MIN_GROUP or len(miss) < MIN_GROUP:
        return {
            "label": label,
            "verdict": "样本不足",
            "hit_rate_pct": round(100.0 * len(hit) / len(rows), 2) if rows else None,
            "n_hit": len(hit),
            "n_miss": len(miss),
        }
    paired, days, positive = _paired_diff(rows, flag)
    band = _noise_band(rows, flag)
    inside = None
    if paired is not None and band[0] is not None:
        # bool() 而非直接比较：pandas 比较结果是 numpy.bool_，json 不认。
        inside = bool(band[0] <= paired <= band[1])
    return {
        "label": label,
        "n_hit": len(hit),
        "n_miss": len(miss),
        "hit_rate_pct": round(100.0 * len(hit) / len(rows), 2),
        "hit_ret": round(_day_weighted(hit), 4),
        "miss_ret": round(_day_weighted(miss), 4),
        "paired_diff": None if paired is None else round(paired, 4),
        "paired_days": days,
        "paired_positive_pct": None if positive is None else round(positive, 2),
        "noise_band": list(band),
        "inside_noise": inside,
        "verdict": _verdict(paired, inside),
    }


def _verdict(paired: float | None, inside: bool | None) -> str:
    if paired is None:
        return "样本不足"
    if inside:
        return "落在噪声带内：无区分力"
    return "正贡献、超出噪声" if paired > 0 else "负贡献、超出噪声"


def main() -> int:
    args = parse_args()
    rows = collect_events(args)
    if not rows:
        print("[c-ablation] 无事件，检查区间与样本量")
        return 1
    report = build_report(rows)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "springboard_c_ablation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def collect_events(args: argparse.Namespace) -> list[dict[str, Any]]:
    """对每个标的每个交易日，算各容差档下的 C 命中与前瞻 5 日收益。"""
    from core.signal_confirmation import compute_support_level
    from integrations.fetch_a_share_csv import get_stocks_by_board
    from workflows.backtest_data import fetch_online_history_map

    codes = [str(item.get("code", "")).strip() for item in get_stocks_by_board("all")]
    codes = [code for code in codes if code][: max(int(args.max_symbols), 1)]
    fetch_start = (pd.to_datetime(args.start) - pd.Timedelta(days=260)).date()
    hist, failures = fetch_online_history_map(codes, fetch_start, pd.to_datetime(args.end).date(), max_workers=8)
    print(f"[c-ablation] 标的 {len(hist)}/{len(codes)}（失败 {len(failures)}）")

    start = pd.to_datetime(args.start).date()
    end = pd.to_datetime(args.end).date()
    rows: list[dict[str, Any]] = []
    for code, frame in hist.items():
        if frame is None or len(frame) < 120:
            continue
        work = frame.reset_index(drop=True)
        dates = pd.to_datetime(work["date"], errors="coerce").dt.date
        close = pd.to_numeric(work["close"], errors="coerce")
        atr_series = atr(work)
        for i in range(100, len(work) - 5):
            day = dates.iloc[i]
            if day is None or not (start <= day <= end):
                continue
            base = float(close.iloc[i])
            atr_value = atr_series.iloc[i]
            if base <= 0 or pd.isna(atr_value) or atr_value <= 0:
                continue
            slice_df = work.iloc[: i + 1]
            # 用非 sos 的默认口径（近 20 日最低价）作为真支撑，sos 另行分层。
            support = compute_support_level(slice_df, "trend_pullback", SUPPORT_WINDOW)
            if support <= 0:
                continue
            low = slice_df["low"]
            entry = {
                "code": code,
                "date": day.isoformat(),
                "ret": (float(close.iloc[i + 5]) / base - 1.0) * 100.0,
                "c_fixed": count_touches(low, support, support * 0.05) >= TOUCH_MIN,
            }
            for k in K_GRID:
                entry[f"c_atr_{k}"] = count_touches(low, support, float(atr_value) * k) >= TOUCH_MIN
            rows.append(entry)
    print(f"[c-ablation] 事件 {len(rows)}，标的 {len({r['code'] for r in rows})}")
    return rows


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    variants = {"fixed_5pct": evaluate_variant(rows, "c_fixed", "固定 5%（现状）")}
    for k in K_GRID:
        variants[f"atr_k{k}"] = evaluate_variant(rows, f"c_atr_{k}", f"ATR×{k}")
    frame = pd.DataFrame(rows)
    split = sorted(frame.date.unique())
    mid = split[len(split) // 2] if split else ""
    oos = {}
    for key, flag in [("fixed_5pct", "c_fixed"), *[(f"atr_k{k}", f"c_atr_{k}") for k in K_GRID]]:
        first = [r for r in rows if r["date"] < mid]
        second = [r for r in rows if r["date"] >= mid]
        oos[key] = {
            "in_sample": evaluate_variant(first, flag, "前段").get("paired_diff"),
            "out_of_sample": evaluate_variant(second, flag, "后段").get("paired_diff"),
        }
    return {
        "events": len(rows),
        "symbols": len({r["code"] for r in rows}),
        "window": {"start": min(frame.date), "end": max(frame.date)},
        "note": (
            "sos 的 support 取 21 日最高价（阻力位），语义上不适用低点触碰支撑；"
            "本报告统一用近 20 日最低价口径，sos 需单独处理，不要用本结论直接调 sos。"
        ),
        "variants": variants,
        "out_of_sample_paired_diff": oos,
        "split_date": mid,
        "hit_rate_summary": {
            key: value.get("hit_rate_pct") for key, value in variants.items() if value.get("hit_rate_pct") is not None
        },
        "median_touch_context": {
            "target": "命中率应显著低于现状的 92%，否则仍是恒真标签",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
