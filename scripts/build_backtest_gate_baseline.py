"""Build the live-vs-off execution-gate baseline report from Backtest Grid artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.backtest_gate_baseline import build_gate_baseline, load_gate_baseline_rows, render_gate_baseline


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总执行闸门 live/off 基线回测")
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    ignored_dirs: list[str] = []
    report = build_gate_baseline(load_gate_baseline_rows(args.artifacts_dir, ignored_dirs), ignored_dirs)
    for directory in ignored_dirs:
        print(f"[gate-baseline] 忽略无法归组的产物目录: {directory}")
    for gate, verdict in report["verdict"].items():
        print(f"[gate-baseline] {gate}: {verdict}")
    args.markdown_output.write_text(render_gate_baseline(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(args.require_complete and report["status"] != "ready")


if __name__ == "__main__":
    raise SystemExit(main())
