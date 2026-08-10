"""CLI entrypoint for as-of US/HK recommendation backfill."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401

from workflows.market_funnel_backfill import MarketBackfillRequest, run_market_funnel_backfill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按历史交易日回刷美股/港股推荐表（as-of 口径）")
    parser.add_argument("--market", choices=["us", "hk"], required=True)
    parser.add_argument("--dates", required=True, help="逗号分隔 YYYY-MM-DD，必须是已收盘的交易日")
    parser.add_argument("--output-dir", default="artifacts/funnel_backfill", help="artifact 输出目录")
    parser.add_argument("--apply", action="store_true", help="确认写库；默认只生成 artifact")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dates = tuple(_parse_date(item) for item in args.dates.split(",") if item.strip())
    _reject_future_dates(dates)
    return run_market_funnel_backfill(
        MarketBackfillRequest(
            market=args.market,
            dates=dates,
            output_dir=args.output_dir,
            apply=bool(args.apply),
        )
    )


def _parse_date(raw: str) -> date:
    return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()


def _reject_future_dates(dates: tuple[date, ...]) -> None:
    """当天及未来一律拒绝：盘中 K 线未收盘，写进去的是残缺 bar。"""
    cutoff = date.today() - timedelta(days=1)
    future = [day.isoformat() for day in dates if day > cutoff]
    if future:
        raise ValueError(f"目标日期必须是已收盘的过去交易日，收到: {','.join(future)}")


if __name__ == "__main__":
    raise SystemExit(main())
