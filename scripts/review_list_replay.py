"""CLI entrypoint for limit-up miss replay."""

from __future__ import annotations

import argparse
import os

import _bootstrap  # noqa: F401

from workflows.review_list_replay import resolve_review_dates, run_review_list_replay


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review strong movers against the previous production funnel")
    parser.add_argument("--print-previous-trade-date", action="store_true")
    return parser.parse_args()


def main() -> int:
    if parse_args().print_previous_trade_date:
        print(resolve_review_dates().previous_trade_date.isoformat())
        return 0
    return run_review_list_replay(os.getenv("FEISHU_WEBHOOK_URL", "").strip())


if __name__ == "__main__":
    raise SystemExit(main())
