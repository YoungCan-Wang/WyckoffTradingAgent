"""按自定义形态口径检验前瞻收益：这类票「抓到了」到底赚不赚钱。

在为召回缺口放宽通道门槛之前先跑它。与 diagnose_funnel_recall.py 互补——那个按代码
逐票回答「为什么没进候选池」，这个按形态口径回答「这类票值不值得进」。

买点固定为 T+1 开盘：漏斗在收盘后才出信号，实盘最早只能次日开盘介入。
基准是同日、同流动性门槛下的全市场等权，故结论是**超额**而非绝对收益。

用法::

    # 默认口径（T-1涨<3%、T开盘<=4%、T涨>7%）
    python scripts/evaluate_pattern_forward.py

    # 自定义：更严的开盘缺口与更高的当日涨幅
    python scripts/evaluate_pattern_forward.py --open-gap-max 3 --day-return-min 8

    # 扫一组阈值组合做对比
    python scripts/evaluate_pattern_forward.py --sweep-gap 2,3,4 --sweep-return 7,8,10

    # 顺带看日内（T 开盘买、T 收盘卖）——注意那是未来函数，仅作参考
    python scripts/evaluate_pattern_forward.py --with-intraday
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.pattern_forward_eval import (
    ROUND_TRIP_COST_PCT,
    PatternReport,
    PatternSpec,
    summarize_horizon,
)

WARMUP = 210


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="形态前瞻收益检验")
    parser.add_argument("--start", default="2024-08-01", help="行情起始（需留 210 日预热）")
    parser.add_argument("--prev-day-max", type=float, default=3.0, help="T-1 日涨幅上限 %%")
    parser.add_argument("--open-gap-max", type=float, default=4.0, help="T 日开盘缺口上限 %%")
    parser.add_argument("--day-return-min", type=float, default=7.0, help="T 日涨幅下限 %%")
    parser.add_argument("--min-amount-wan", type=float, default=8000.0, help="20 日均额下限（万元）")
    parser.add_argument("--horizons", default="5,10", help="持有期，逗号分隔")
    parser.add_argument("--sweep-gap", default="", help="扫开盘缺口，如 2,3,4")
    parser.add_argument("--sweep-return", default="", help="扫当日涨幅，如 7,8,10")
    parser.add_argument("--with-intraday", action="store_true", help="附带日内对照（未来函数）")
    parser.add_argument("--cache", default="", help="行情缓存 CSV，跳过在线取数")
    parser.add_argument("--json-out", default="", help="结构化结果输出路径")
    return parser.parse_args()


def load_market(start: str, cache: str) -> pd.DataFrame:
    if cache and Path(cache).exists():
        print(f"[pattern] 使用缓存 {cache}")
        frame = pd.read_csv(cache, dtype={"ts_code": str, "trade_date": str})
    else:
        from integrations.fetch_a_share_csv import cached_trade_dates
        from integrations.tushare_client import get_pro

        pro = get_pro()
        if pro is None:
            raise SystemExit("需要 TUSHARE_TOKEN，或用 --cache 指定行情 CSV")
        end = pd.Timestamp.now().strftime("%Y-%m-%d")
        days = [str(d) for d in cached_trade_dates() if start <= str(d) <= end]
        frames = []
        for day in days:
            try:
                got = pro.daily(trade_date=day.replace("-", ""), fields="ts_code,trade_date,open,close,amount")
                if got is not None and not got.empty:
                    frames.append(got)
            except Exception as exc:  # noqa: BLE001 - 单日失败不中断
                print(f"[pattern] {day} 取数失败: {str(exc)[:60]}")
        if not frames:
            raise SystemExit("未取到行情")
        frame = pd.concat(frames)
    frame["d"] = pd.to_datetime(frame.trade_date, format="%Y%m%d")
    return frame.sort_values(["ts_code", "d"])


def _pivots(market: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    close = market.pivot_table(index="d", columns="ts_code", values="close")
    open_ = market.pivot_table(index="d", columns="ts_code", values="open")
    amount = market.pivot_table(index="d", columns="ts_code", values="amount")
    return close, open_, amount


def evaluate(market: pd.DataFrame, spec: PatternSpec, *, with_intraday: bool = False) -> tuple[PatternReport, dict]:
    close, open_, amount = _pivots(market)
    dates = list(close.index)
    sink: dict[int, list[dict[str, float]]] = {h: [] for h in spec.horizons}
    intraday: list[float] = []
    max_h = max(spec.horizons)

    for i in range(WARMUP, len(dates) - max_h - 1):
        frame = pd.DataFrame(
            {
                "c": close.iloc[i],
                "p1": close.iloc[i - 1],
                "p2": close.iloc[i - 2],
                "o": open_.iloc[i],
                # 只用 T-1 及之前的成交额，避免用到当日信息。
                "amt": amount.iloc[i - 20 : i].mean() / 10.0,
                "nxo": open_.iloc[i + 1],
            }
        ).dropna()
        frame = frame[(frame.p1 > 0) & (frame.p2 > 0) & (frame.nxo > 0) & (frame.amt >= spec.min_avg_amount_wan)]
        if len(frame) < 50:
            continue
        frame["ret"] = (frame.c / frame.p1 - 1) * 100
        frame["prev"] = (frame.p1 / frame.p2 - 1) * 100
        frame["gap"] = (frame.o / frame.p1 - 1) * 100
        hit = frame[
            (frame.ret > spec.day_return_min_pct)
            & (frame.prev < spec.prev_day_max_pct)
            & (frame.gap <= spec.open_gap_max_pct)
        ]
        if hit.empty:
            continue
        if with_intraday:
            intraday.append(float(((hit.c / hit.o - 1) * 100).mean()) - ROUND_TRIP_COST_PCT)
        for horizon in spec.horizons:
            exit_idx = i + 1 + horizon
            if exit_idx >= len(dates):
                continue
            exit_px = close.iloc[exit_idx]
            net = float((exit_px / hit.nxo - 1).mean() * 100) - ROUND_TRIP_COST_PCT
            market_ret = float((exit_px / frame.nxo - 1).mean() * 100)
            sink[horizon].append({"net": net, "market": market_ret, "hits": float(len(hit))})

    report = PatternReport(spec=spec)
    report.results = [summarize_horizon(h, sink[h]) for h in spec.horizons]
    extra = {}
    if intraday:
        extra["intraday"] = {
            "days": len(intraday),
            "mean_pct": round(sum(intraday) / len(intraday), 4),
            "positive_day_pct": round(100.0 * sum(1 for v in intraday if v > 0) / len(intraday), 1),
            "caveat": "未来函数：用当日涨幅筛票再算当日收益，实盘开盘时不可知",
        }
    return report, extra


def render(report: PatternReport, extra: dict) -> str:
    lines = [
        "**形态前瞻收益检验**",
        "",
        f"口径　{report.spec.describe()}",
        f"买点　T+1 开盘（漏斗收盘后出信号，最早次日介入）　成本 {ROUND_TRIP_COST_PCT}%",
        "",
        "| 持有 | 天数 | 日均只数 | 净收益 | 市场 | 净超额 | 为正日% | 判定 |",
        "| --- | --: | --: | --: | --: | --: | --: | --- |",
    ]
    for r in report.results:
        if r.net_excess_pct is None:
            lines.append(f"| T+{r.horizon} | {r.days} | — | — | — | — | — | {r.verdict} |")
            continue
        lines.append(
            f"| T+{r.horizon} | {r.days} | {r.avg_hits:.0f} | {r.net_return_pct:+.2f}% | "
            f"{r.market_return_pct:+.2f}% | {r.net_excess_pct:+.2f} | {r.positive_day_pct:.0f}% | {r.verdict} |"
        )
    if "intraday" in extra:
        i = extra["intraday"]
        lines += [
            "",
            f"日内对照　T 开盘买 / T 收盘卖：{i['mean_pct']:+.2f}%，{i['days']} 天中 {i['positive_day_pct']:.0f}% 为正",
            f"　⚠️ {i['caveat']}",
        ]
    lines += ["", "**结论**"]
    if report.any_actionable:
        best = max((r for r in report.results if r.net_excess_pct is not None), key=lambda r: r.net_excess_pct)
        lines.append(
            f"- 净超额 {best.net_excess_pct:+.2f}pct 已过成本门槛 {ROUND_TRIP_COST_PCT}%，"
            f"该形态值得考虑放宽对应通道；但仍需跨行情段确认后再改参数。"
        )
    else:
        lines.append(
            f"- 所有持有期的净超额均未过成本门槛 {ROUND_TRIP_COST_PCT}%，"
            f"**不应为该形态放宽通道门槛**——放宽只会引入更多负 alpha 标的。"
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    market = load_market(args.start, args.cache)
    print(f"[pattern] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")

    gaps = [float(x) for x in args.sweep_gap.split(",") if x.strip()] or [args.open_gap_max]
    rets = [float(x) for x in args.sweep_return.split(",") if x.strip()] or [args.day_return_min]
    payload: list[dict] = []
    for gap in gaps:
        for ret in rets:
            spec = PatternSpec(
                prev_day_max_pct=args.prev_day_max,
                open_gap_max_pct=gap,
                day_return_min_pct=ret,
                min_avg_amount_wan=args.min_amount_wan,
                horizons=horizons,
            )
            report, extra = evaluate(market, spec, with_intraday=args.with_intraday)
            print()
            print(render(report, extra))
            payload.append({**report.as_dict(), **extra})
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[pattern] 已写 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
