"""CLI entrypoint for HK backtest notification."""

from __future__ import annotations

import argparse

from workflows.backtest_notification import BacktestNotifyRequest, run_backtest_notification


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate HK backtest summary artifacts and notify Feishu.")
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--output", default="artifacts/BACKTEST_HK_REPORT.md")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--top-n", default="2")
    return parser.parse_args()


def request_from_args(args: argparse.Namespace) -> BacktestNotifyRequest:
    return BacktestNotifyRequest(
        market="hk",
        artifacts_dir=args.artifacts_dir,
        output=args.output,
        run_url=args.run_url,
        top_n=args.top_n,
    )


def main() -> int:
    return run_backtest_notification(request_from_args(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
