"""Build A/M/P backtest comparison artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.backtest_strategy_comparison import (
    build_strategy_comparison,
    load_strategy_comparison_rows,
    render_strategy_comparison,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总策略 A/M/P 消融回测")
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    report = build_strategy_comparison(load_strategy_comparison_rows(args.artifacts_dir))
    args.markdown_output.write_text(render_strategy_comparison(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(args.require_complete and report["status"] != "ready")


if __name__ == "__main__":
    raise SystemExit(main())
