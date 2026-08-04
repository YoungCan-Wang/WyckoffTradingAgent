"""CLI: 按首次推荐日收盘价纠偏 recommendation_tracking.initial_price。"""

from __future__ import annotations

import argparse
import json
import os

import _bootstrap  # noqa: F401

from integrations.supabase_base import WRITE_CONTEXT_ENV
from workflows.recommendation_tracking_reprice import correct_tracking_initial_prices


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将推荐价 initial_price 回刷为该股票首次 recommend_date 收盘价（默认 dry-run）"
    )
    parser.add_argument("--apply", action="store_true", help="确认写库；默认只预览将改写的行")
    parser.add_argument("--sample-limit", type=int, default=10, help="预览样例条数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        os.environ.setdefault(WRITE_CONTEXT_ENV, "server_job")
    summary = correct_tracking_initial_prices(apply=bool(args.apply))
    samples = list(summary.get("samples") or [])[: max(int(args.sample_limit), 0)]
    print(
        json.dumps(
            {
                "apply": bool(args.apply),
                "rows_total": summary.get("rows_total", 0),
                "rows_changed": summary.get("rows_changed", 0),
                "rows_written": summary.get("rows_written", 0),
                "samples": samples,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
