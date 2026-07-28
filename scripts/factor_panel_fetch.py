"""CLI entrypoint: build the point-in-time factor panel."""

from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from integrations.factor_panel import build_panel


def main() -> int:
    parser = argparse.ArgumentParser(description="按交易日横切拉取 PIT 因子面板（含退市与 ST）")
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--output-dir", type=Path, default=Path("factor_panel"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    build_panel(
        args.start.replace("-", ""),
        args.end.replace("-", ""),
        args.output_dir,
        workers=args.workers,
        progress=print,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
