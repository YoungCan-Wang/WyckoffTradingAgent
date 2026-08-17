"""每日净值快照作业，兼历史空洞回补。

净值是账户事实，不该因为漏斗或 Step4 失败而缺失。本作业独立于信号链路运行。

用法::

    python scripts/nav_snapshot_job.py                          # 记录今天
    python scripts/nav_snapshot_job.py --check 2026-07-30 2026-08-17   # 只报空洞
    python scripts/nav_snapshot_job.py --date 2026-08-17        # 指定日期（dry-run）
    python scripts/nav_snapshot_job.py --date 2026-08-17 --apply

写入需要 ``WYCKOFF_WRITE_CONTEXT=server_job``；默认 dry-run，只打印不落库。

**不回补历史行情**：``build_nav_snapshot`` 用的是**最新**报价，所以指定过去日期只在
当天盘后补记当日净值时才正确。真正的历史空洞（如 08-07/08-10）无法用现价重算——
那需要按当日收盘价重估，而 TickFlow 的历史快照不保证可得。故 --check 只报告缺口，
不假装能把它们填上：宁可留下可见的空洞，也不要写入一个用错价格算出的净值。
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime

import _bootstrap  # noqa: F401

from utils.trading_clock import CN_TZ


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日净值快照")
    parser.add_argument("--portfolio-id", default="", help="组合 ID，默认取 MY_PORTFOLIO_ID 或 USER_LIVE")
    parser.add_argument("--date", default="", help="交易日 YYYY-MM-DD，默认今天（北京时间）")
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省仅打印")
    parser.add_argument("--check", nargs=2, metavar=("START", "END"), help="报告区间内缺失净值的交易日")
    return parser.parse_args()


def _default_portfolio_id() -> str:
    for key in ("MY_PORTFOLIO_ID", "PORTFOLIO_ID"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return "USER_LIVE"


def main() -> int:
    args = parse_args()
    from utils.runtime_friction import apply_friction_config_from_env

    apply_friction_config_from_env()
    portfolio_id = args.portfolio_id.strip() or _default_portfolio_id()

    if args.check:
        from workflows.nav_snapshot import missing_nav_dates

        start, end = args.check
        missing = missing_nav_dates(portfolio_id, start, end)
        total = len(missing)
        print(f"[nav] {portfolio_id} {start}~{end} 缺失 {total} 个交易日")
        for day in missing:
            print(f"  - {day}")
        if total:
            print("\n注：历史空洞需按当日收盘价重估，现价重算会写入错误净值，故此处只报告不回补。")
        return 0

    trade_date = args.date.strip() or datetime.now(CN_TZ).date().isoformat()
    from workflows.nav_snapshot import build_nav_snapshot, persist_nav_snapshot

    snapshot = build_nav_snapshot(portfolio_id, trade_date)
    if not snapshot.ok:
        print(f"[nav] 快照失败 {portfolio_id} {trade_date}: {snapshot.message}")
        return 1
    print(f"[nav] {portfolio_id} {trade_date}  {snapshot.message}")
    print(
        f"      total_equity={snapshot.total_equity:,.2f}  "
        f"free_cash={snapshot.free_cash:,.2f}  positions_value={snapshot.positions_value:,.2f}"
    )
    if not args.apply:
        print("[nav] dry-run，未写库；加 --apply 落库")
        return 0
    result = persist_nav_snapshot(snapshot)
    print("[nav] 已写入 daily_nav" if result.written else "[nav] 写入失败")
    return 0 if result.written else 1


if __name__ == "__main__":
    raise SystemExit(main())
