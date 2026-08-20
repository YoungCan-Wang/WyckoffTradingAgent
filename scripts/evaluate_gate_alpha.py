"""门槛层 alpha 检验：跑数、落盘、推飞书。

回答两个问题：
1. L3「只买热门行业」这一层有没有正向选股能力？
2. 止损的跟踪参考价（60 日最高）陈旧到什么程度时，止损开始变成错的？

首轮（2026-08-20，107 个交易日）结论：
- 题材共振**越热越差**：生产 topN=5 的差值 -0.37pct，放宽到 20 才转正 +0.11pct，
  而增益小于单次往返成本 0.202%，故未改。
- 止损各偏离档超额**全为负**（-0.51 / -0.54 / -0.35 / -0.07），说明止损触发后确实
  继续跑输大盘。江顺科技 2026-08-18 参考价偏离 +52% 而次日涨 10% 属个案，
  故明确**不改风控**。

用法::

    python scripts/evaluate_gate_alpha.py --horizon 5
    python scripts/evaluate_gate_alpha.py --horizon 5 --no-notify
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.gate_alpha_eval import (
    MIN_GROUP,
    PROD_RECENT_HIGH_WINDOW,
    PROD_TOP_N_SECTORS,
    PROD_TRAILING_DRAWDOWN_PCT,
    STALE_BANDS,
    TOP_N_GRID,
    GateReport,
    band_of,
    render,
    summarize,
)

WARMUP_BARS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="门槛层 alpha 检验")
    parser.add_argument("--horizon", type=int, default=5, help="前瞻交易日数")
    parser.add_argument("--start", default="2025-11-01", help="行情起始（需留足 60 日预热）")
    parser.add_argument("--out", default="docs/evidence", help="产物目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def load_market(start: str) -> pd.DataFrame:
    """全市场日线。按交易日批量取，逐只取会慢一个数量级。"""
    from integrations.fetch_a_share_csv import cached_trade_dates
    from integrations.tushare_client import get_pro

    pro = get_pro()
    if pro is None:
        raise SystemExit("需要 TUSHARE_TOKEN")
    end = pd.Timestamp.now().strftime("%Y-%m-%d")
    days = [str(day) for day in cached_trade_dates() if start <= str(day) <= end]
    frames = []
    for day in days:
        try:
            frame = pro.daily(trade_date=day.replace("-", ""), fields="ts_code,trade_date,high,close")
            if frame is not None and not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001 - 单日失败不应中断整体检验
            print(f"[gate] {day} 取数失败: {str(exc)[:60]}")
    if not frames:
        raise SystemExit("未取到行情")
    market = pd.concat(frames)
    market["d"] = pd.to_datetime(market.trade_date, format="%Y%m%d")
    return market.sort_values(["ts_code", "d"])


def build_report(market: pd.DataFrame, sector_map: dict[str, str], horizon: int) -> GateReport:
    close = market.pivot_table(index="d", columns="ts_code", values="close")
    high = market.pivot_table(index="d", columns="ts_code", values="high")
    dates = list(close.index)
    theme_daily: dict[int, list[dict[str, float]]] = {top_n: [] for top_n in TOP_N_GRID}
    stop_daily: dict[str, list[dict[str, float]]] = {label: [] for _, _, label in STALE_BANDS}

    for i in range(WARMUP_BARS, len(dates) - horizon):
        spot = close.iloc[i]
        forward = (close.iloc[i + horizon] / spot - 1.0) * 100.0
        momentum = (spot / close.iloc[i - 5] - 1.0) * 100.0
        frame = pd.DataFrame({"c": spot, "r5": momentum, "fwd": forward}).dropna()
        frame = frame[frame.c > 0]
        if len(frame) < 50:
            continue
        market_ret = float(frame.fwd.mean())
        _collect_theme(frame, sector_map, theme_daily, market_ret)
        _collect_stop(frame, high, close, i, stop_daily, market_ret)

    report = GateReport()
    report.theme = [
        summarize(f"topN={top_n}", theme_daily[top_n], is_production=top_n == PROD_TOP_N_SECTORS)
        for top_n in TOP_N_GRID
    ]
    report.stop_loss = [summarize(label, stop_daily[label]) for _, _, label in STALE_BANDS]
    return report


def _collect_theme(
    frame: pd.DataFrame,
    sector_map: dict[str, str],
    sink: dict[int, list[dict[str, float]]],
    market_ret: float,
) -> None:
    work = frame.copy()
    work["code"] = [str(x).split(".")[0] for x in work.index]
    work["ind"] = work.code.map(sector_map)
    work = work.dropna(subset=["ind"])
    if work.empty:
        return
    strength = work.groupby("ind").r5.mean().sort_values(ascending=False)
    for top_n in sink:
        hot = set(strength.head(top_n).index)
        inside = work[work.ind.isin(hot)]
        outside = work[~work.ind.isin(hot)]
        if len(inside) >= MIN_GROUP and len(outside) >= MIN_GROUP:
            sink[top_n].append(
                {"inside": float(inside.fwd.mean()), "outside": float(outside.fwd.mean()), "size": float(len(inside))}
            )
    del market_ret  # 题材层用「热门 vs 非热门」直接对照，不需要市场基准


def _collect_stop(
    frame: pd.DataFrame,
    high: pd.DataFrame,
    close: pd.DataFrame,
    index: int,
    sink: dict[str, list[dict[str, float]]],
    market_ret: float,
) -> None:
    """复刻生产止损：trailing = 60日最高 × (1 + drawdown)，并与 MA50×0.98 取高者。"""
    window_high = high.iloc[index - (PROD_RECENT_HIGH_WINDOW - 1) : index + 1].max()
    ma50 = close.iloc[index - 49 : index + 1].mean()
    work = frame.join(pd.DataFrame({"h60": window_high, "ma50": ma50}), how="inner").dropna()
    work = work[(work.h60 > 0) & (work.ma50 > 0)]
    if work.empty:
        return
    trailing = work.h60 * (1.0 + PROD_TRAILING_DRAWDOWN_PCT / 100.0)
    stop_price = pd.concat([trailing, work.ma50 * 0.98], axis=1).max(axis=1)
    fired = work[work.c <= stop_price].copy()
    if fired.empty:
        return
    fired["dev"] = (fired.h60 / fired.c - 1.0) * 100.0
    fired["band"] = fired.dev.map(band_of)
    for label, group in fired.dropna(subset=["band"]).groupby("band"):
        if len(group) >= MIN_GROUP:
            sink[str(label)].append(
                {"inside": float(group.fwd.mean()), "outside": market_ret, "size": float(len(group))}
            )


def main() -> int:
    args = parse_args()
    from integrations.market_metadata import fetch_sector_map

    sector_map = fetch_sector_map()
    print(f"[gate] 行业映射 {len(sector_map)} 只")
    market = load_market(args.start)
    print(f"[gate] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    report = build_report(market, sector_map, max(int(args.horizon), 1))
    payload = report.as_dict()
    payload["horizon_days"] = int(args.horizon)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"gate_alpha_h{args.horizon}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(report)
    print(text)
    if not args.no_notify:
        _notify(text, int(args.horizon))
    return 0


def _notify(markdown: str, horizon: int) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[gate] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"门槛层 alpha 检验｜T+{horizon}｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[gate] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[gate] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
