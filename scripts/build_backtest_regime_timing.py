"""Build the regime-as-pure-timing index report from Backtest Grid gate artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.backtest_regime_timing import build_regime_timing, load_regime_timing_inputs, render_regime_timing


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总水温纯择时指数基线")
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    args = parser.parse_args()
    report = build_regime_timing(load_regime_timing_inputs(args.artifacts_dir))
    print(f"[regime-timing] status={report['status']} production_gate 在场: {report['production_gate_allowed']}")
    for variant, verdict in report["verdict"].items():
        print(f"[regime-timing] {variant}: {verdict['status']}")
    args.markdown_output.write_text(render_regime_timing(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
