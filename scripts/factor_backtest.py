"""CLI entrypoint: cross-sectional factor portfolio backtest."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401

from core.cash_portfolio import CashPortfolioConfig
from core.factor_portfolio import FactorPortfolioConfig
from workflows.factor_backtest import load_prepared_panel, run_sweep, write_artifacts


def _int_list(raw: str) -> list[int]:
    return [int(item) for item in str(raw).split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="截面价值组合回测：合成分排序 + 缓冲式再平衡 + 真实摩擦")
    parser.add_argument("--panel-dir", type=Path, default=Path("factor_panel"))
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--top-n", default="30,50,100", help="持仓数网格")
    parser.add_argument("--rebalance-days", default="10,20", help="再平衡间隔网格")
    parser.add_argument("--buffer-mult", type=float, default=2.0, help="跌出 top_n×该倍数才卖出")
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--slippage-bps", type=float, default=10.0, help="单边滑点，基点")
    parser.add_argument("--exclude-st", action="store_true", help="按 PIT 名称剔除 ST，而不是按当前名称")
    parser.add_argument("--min-amount-thousand", type=float, default=500.0, help="当日成交额下限（千元，tushare 口径）")
    parser.add_argument("--min-listed-days", type=int, default=120, help="上市未满该天数不参与选股")
    parser.add_argument("--output-dir", type=Path, default=Path("analysis/factor_backtest"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    panel = load_prepared_panel(
        args.panel_dir,
        start=args.start,
        end=args.end,
        exclude_st=args.exclude_st,
        min_amount_thousand=args.min_amount_thousand,
        min_listed_days=args.min_listed_days,
    )
    print(f"面板 {len(panel)} 行 / {panel['symbol'].nunique()} 只 / {panel['date'].nunique()} 天")

    base = FactorPortfolioConfig(
        buffer_mult=args.buffer_mult,
        slippage_bps=args.slippage_bps,
        costs=CashPortfolioConfig(initial_cash=args.initial_cash),
    )
    configs = [
        replace(base, top_n=n, rebalance_days=k) for n in _int_list(args.top_n) for k in _int_list(args.rebalance_days)
    ]
    matrix, detail = run_sweep(panel, configs, progress=print)
    out = write_artifacts(args.output_dir, matrix, detail, base)
    print(f"产物写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
