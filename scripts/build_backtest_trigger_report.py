"""Aggregate per-period parameter-sweep matrices into a cross-period verdict."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.backtest_trigger_matrix import (
    build_trigger_report,
    load_trigger_matrix_rows,
    render_trigger_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总参数矩阵：触发阈值做 walk-forward 选值，top_n 做选择层增益对比")
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--require-pass", action="store_true", help="样本外未占优时以非零码退出")
    args = parser.parse_args()
    report = build_trigger_report(load_trigger_matrix_rows(args.artifacts_dir))
    args.markdown_output.write_text(render_trigger_report(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_trigger_report(report))
    return int(args.require_pass and report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
