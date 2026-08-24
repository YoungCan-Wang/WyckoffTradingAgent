"""IC 反向打分影子池：每日取 top-N 写 observation，只观察不下单。

## 为什么需要它

2026-08-22 的 IC 扫描显示生产四条通道方向都反了（主升 rps_slow>=75 的 IC 是
-0.0697、加速突破的放量 vol_ratio 是 -0.0262），且该结论在 3 段样本上方向全一致
——是当日唯一跨段稳定的发现。

但不能据此直接改八通道：IC 只说明方向反了，不说明该设什么阈值，而阈值化本身就是
过拟合来源（参数网格 walk-forward 仅 1/16 个窗口为正）。故先并行跑影子池，
用 source='ic_shadow' 写入 signal_observations，两三周后与现有通道对比再决定。

写入复用既有表而非新建：signal_observations 已有 channel / priority_score /
features_json 字段，且 source 列已有 shadow_added / shadow_removed 等用法。

用法::

    # 干跑，只打印不写库
    python scripts/run_ic_shadow_pool.py --cache /tmp/snap/hist_full.csv.gz --dry-run

    # 正式写入
    python scripts/run_ic_shadow_pool.py --cache /tmp/snap/hist_full.csv.gz

    # 用自定义权重（默认取首轮 IC 扫描结果）
    python scripts/run_ic_shadow_pool.py --weights ret60=-0.37,dry_vol_q250=-0.31
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
import pandas as pd

from core.ic_shadow_score import (
    SHADOW_SOURCE,
    FactorWeight,
    ShadowScoreConfig,
    combine_scores,
    to_rows,
)

WARMUP = 260


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IC 反向打分影子池")
    p.add_argument("--cache", required=True, help="行情快照（hist_full.csv.gz 或 tushare CSV）")
    p.add_argument("--trade-date", default="", help="信号日 YYYY-MM-DD，默认取行情最后一日")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--min-amount-wan", type=float, default=8000.0)
    p.add_argument("--weights", default="", help="因子=权重，逗号分隔；留空用首轮 IC 默认值")
    p.add_argument("--dry-run", action="store_true", help="只打印不写库")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> ShadowScoreConfig:
    if not args.weights:
        return ShadowScoreConfig(top_n=args.top_n, min_avg_amount_wan=args.min_amount_wan)
    parsed: list[FactorWeight] = []
    for item in args.weights.split(","):
        if not item.strip():
            continue
        name, _, raw = item.partition("=")
        parsed.append(FactorWeight(name.strip(), float(raw)))
    if not parsed:
        raise SystemExit("--weights 解析为空")
    return ShadowScoreConfig(weights=tuple(parsed), top_n=args.top_n, min_avg_amount_wan=args.min_amount_wan)


def compute_percentiles(
    market: pd.DataFrame, config: ShadowScoreConfig, trade_date: str
) -> tuple[dict[str, dict[str, float]], str, int]:
    """算出信号日各因子的横截面分位。返回 (面板, 实际信号日, 参与标的数)。

    因子构造与 scripts/scan_factor_ic.py 保持一致，否则影子池打分与 IC 结论会脱节。
    """
    from scripts.scan_factor_ic import build_factors

    factors, close, _open = build_factors(market)
    dates = list(close.index)
    if len(dates) <= WARMUP:
        raise SystemExit(f"行情不足：需 >{WARMUP} 个交易日，实际 {len(dates)}")
    if trade_date:
        target = pd.Timestamp(trade_date)
        if target not in close.index:
            raise SystemExit(f"{trade_date} 不在行情交易日内")
        idx = dates.index(target)
    else:
        idx = len(dates) - 1
    liquidity = (factors["turnover_amt"] / 10.0).iloc[idx]
    eligible = liquidity[liquidity >= config.min_avg_amount_wan].index

    panels: dict[str, dict[str, float]] = {}
    for name in config.normalized():
        if name not in factors:
            raise SystemExit(f"未知因子 {name}；可选 {sorted(factors)}")
        row = factors[name].iloc[idx].reindex(eligible).dropna()
        if row.empty:
            continue
        panels[name] = (row.rank(pct=True) * 100).to_dict()
    return panels, str(dates[idx].date()), len(eligible)


def main() -> int:
    args = parse_args()
    config = build_config(args)
    from scripts.scan_factor_ic import load_market

    market = load_market(args.cache, "2024-01-01")
    print(f"[shadow] 行情 {len(market):,} 行 / {market.ts_code.nunique()} 只")
    print(f"[shadow] 配置 {config.describe()}")

    panels, trade_date, universe = compute_percentiles(market, config, args.trade_date)
    picks = combine_scores(panels, config)
    print(f"[shadow] 信号日 {trade_date}　可选标的 {universe}　选出 {len(picks)} 只\n")
    if not picks:
        print("[shadow] 无标的入选")
        return 0

    print(f"{'#':>3} {'代码':12}{'分数':>9}  各因子分位")
    for pick in picks:
        detail = " ".join(f"{k}={v:.0f}" for k, v in pick.factor_ranks.items())
        print(f"{pick.rank:>3} {pick.code:12}{pick.score:>+9.2f}  {detail}")

    rows = to_rows(picks, trade_date, config)
    if args.dry_run:
        print(f"\n[shadow] dry-run：本应写入 {len(rows)} 行")
        return 0
    from integrations.supabase_signal_feedback import upsert_signal_observations

    written = upsert_signal_observations(rows)
    print(f"\n[shadow] 已写入 {written} 行到 signal_observations（source={SHADOW_SOURCE}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
