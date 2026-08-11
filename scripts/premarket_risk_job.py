"""CLI entrypoint for the premarket risk job."""

from __future__ import annotations

import argparse
import os

import _bootstrap  # noqa: F401

from workflows.premarket_risk_job import PremarketRiskJobConfig, default_logs_path, run_premarket_risk_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="盘前风控：A50 + VIX")
    parser.add_argument("--logs", default=None, help="日志文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不推送")
    parser.add_argument("--backstop", action="store_true", help="兜底补跑：当日盘前态已落库则直接退出")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_premarket_risk_job(
        PremarketRiskJobConfig(
            logs_path=args.logs or default_logs_path(),
            webhook=os.getenv("FEISHU_WEBHOOK_URL", "").strip(),
            dry_run=args.dry_run,
            backstop=args.backstop,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
