"""CLI entrypoint for the standalone medium-term Institutional Slow-Bull scanner."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from workflows.slow_bull_scan import SlowBullScanRequest, run_slow_bull_scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="低波机构慢牛（FIP 高 R² 温水煮青蛙）中线扫描")
    parser.add_argument(
        "--codes",
        default="",
        help="可选，逗号分隔 6 位股票代码做定点诊断（如 600887,300498）；留空则做全市场横截面扫描",
    )
    parser.add_argument(
        "--board",
        default="main_chinext_star",
        choices=["all", "main_chinext_star", "main", "chinext", "star", "bse"],
        help="全市场扫描板块范围，默认 main_chinext_star",
    )
    parser.add_argument("--limit", type=int, default=0, help="全市场扫描标的上限（0 为不限制）")
    parser.add_argument("--as-of-date", default="", help="可选时点截止日期 YYYY-MM-DD")
    parser.add_argument("--trading-days", type=int, default=160, help="拉取交易日数量，默认 160")
    parser.add_argument("--skip-financial-metrics", action="store_true", help="跳过 TickFlow 财务质量过滤")
    parser.add_argument(
        "--allow-cycle-cashflow-bypass",
        action="store_true",
        help="允许经营现金流/营收>=10%% 的周期股旁路 ROE>=6%% 门槛",
    )
    parser.add_argument("--top-n", type=int, default=15, help="每档最多保留候选数量，默认 15")
    parser.add_argument("--json-out", default="", help="可选 JSON 报告输出路径")
    parser.add_argument("--md-out", default="", help="可选 Markdown 报告输出路径")
    return parser


def _parse_codes(raw: str) -> tuple[str, ...]:
    items = [part.strip() for part in str(raw or "").replace("，", ",").split(",")]
    return tuple(item for item in items if item)


def _parse_date(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return None
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def request_from_args(args: argparse.Namespace) -> SlowBullScanRequest:
    return SlowBullScanRequest(
        codes=_parse_codes(args.codes),
        board=args.board,
        limit=max(int(args.limit), 0),
        as_of_date=_parse_date(args.as_of_date),
        trading_days=max(int(args.trading_days), 130),
        include_financial_metrics=not bool(args.skip_financial_metrics),
        allow_cycle_cashflow_bypass=bool(args.allow_cycle_cashflow_bypass),
        top_n=max(int(args.top_n), 1),
    )


def main() -> int:
    args = build_parser().parse_args()
    payload = run_slow_bull_scan(request_from_args(args))
    markdown = str(payload.get("markdown_report") or "")
    if markdown:
        print(markdown)
    if args.md_out:
        out_path = Path(args.md_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown + "\n", encoding="utf-8")
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
