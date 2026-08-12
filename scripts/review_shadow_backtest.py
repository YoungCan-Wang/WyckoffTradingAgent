"""CLI for point-in-time Review shadow-lane evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.review_shadow_backtest import run_shadow_backtest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用历史生产 trace 回测 Review 影子召回车道")
    parser.add_argument("--trace-dir", required=True, help="包含 review_trace_YYYYMMDD.json.gz 的目录")
    parser.add_argument("--snapshot-dir", required=True, help="包含 hist_full.csv.gz 的历史快照目录")
    parser.add_argument("--output-dir", default="artifacts/review_shadow_backtest")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_shadow_backtest(Path(args.trace_dir), Path(args.snapshot_dir), Path(args.output_dir))
    print(f"[review-shadow] trace_days={report['trace_days']}, trades={report['trades']}, output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
