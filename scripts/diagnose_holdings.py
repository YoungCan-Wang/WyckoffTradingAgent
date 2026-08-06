"""CLI entrypoint for holding diagnosis."""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from workflows.diagnose_holdings_cli import run_diagnose_holdings_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="持仓健康诊断 CLI — 基于 Wyckoff 引擎的结构化诊断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--codes", type=str, default="", help="逗号分隔的股票代码，如 300813,600703,300014")
    parser.add_argument("--costs", type=str, default="", help="逗号分隔的持仓成本，与 --codes 一一对应")
    parser.add_argument("--names", type=str, default="", help="逗号分隔的股票名称（可选），与 --codes 一一对应")
    parser.add_argument(
        "--buy-dts",
        dest="buy_dts",
        type=str,
        default="",
        help="逗号分隔的建仓日（YYYY-MM-DD，可选），与 --codes 一一对应。"
        "缺失时退出信号基于全历史，建仓前的暴跌可能被误判为破位",
    )
    parser.add_argument("--from-portfolio", type=str, default="", help="从 Supabase 读取持仓，格式 USER_LIVE:<user_id>")
    parser.add_argument("--format", type=str, choices=["text", "markdown", "json"], default="text")
    parser.add_argument("--output", "-o", type=str, default="", help="输出到文件（不指定则输出到终端）")
    return parser


def main() -> int:
    run_diagnose_holdings_cli(build_parser().parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
