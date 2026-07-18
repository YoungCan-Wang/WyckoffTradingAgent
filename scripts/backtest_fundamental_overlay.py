"""Evaluate the research-only fundamental overlay on existing trade artifacts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.fundamental_overlay_backtest import (
    attach_point_in_time_overlay,
    build_overlay_evidence,
    fetch_tickflow_history,
    load_trade_files,
    write_overlay_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Point-in-time fundamental overlay backtest")
    parser.add_argument("--trades-root", required=True, help="Recursively scan for trades_*.csv")
    parser.add_argument("--financial-cache", required=True, help="TickFlow history JSON cache")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = sorted(Path(args.trades_root).rglob("trades_*.csv"))
    trades = load_trade_files(paths)
    if trades.empty:
        raise RuntimeError(f"no trade artifacts under {args.trades_root}")
    cache_path = Path(args.financial_cache)
    if cache_path.exists():
        history = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("TICKFLOW_API_KEY is required when cache is missing")
        history = fetch_tickflow_history(sorted(trades["code"].unique()), api_key)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    enriched = attach_point_in_time_overlay(trades, history)
    evidence = build_overlay_evidence(enriched)
    paths = write_overlay_artifacts(Path(args.output_dir), enriched, evidence)
    print(evidence["decision"]["status"], evidence["decision"]["reason"])
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
