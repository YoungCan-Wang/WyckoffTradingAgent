"""Build the execution-structure baseline report from Backtest Grid exit artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.backtest_exit_baseline import build_exit_baseline, load_exit_baseline_rows, render_exit_baseline


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总执行结构基线（持有期 × 止损有无，wyckoff 对 random）")
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    args = parser.parse_args()
    report = build_exit_baseline(load_exit_baseline_rows(args.artifacts_dir))
    print(f"[exit-baseline] status={report['status']} verdict={report['verdict']['status']}")
    for cell, status in report["separation"].items():
        print(f"[exit-baseline] {cell}: {status}")
    args.markdown_output.write_text(render_exit_baseline(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
