"""CLI entrypoint for announced restructure / halt observation scan."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401

from workflows.corporate_event_scan_runtime import notify_corporate_event_scan, run_corporate_event_scan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描已公告与媒体电报中的重大资产重组 / 停牌")
    parser.add_argument("--output", default="logs/corporate_event_scan.md")
    parser.add_argument("--json-output", default="logs/corporate_event_scan.json")
    parser.add_argument("--as-of", default="", help="观察截止时间，默认上海当前时刻")
    parser.add_argument("--extra-json", default="", help="额外注入的电报 JSON 列表，供回归")
    parser.add_argument("--dry-run", action="store_true", help="只写产物，不推飞书")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    extra = _load_extra(args.extra_json)
    result = run_corporate_event_scan(
        as_of=args.as_of or None,
        extra_items=extra,
        output=args.output,
        json_output=args.json_output,
    )
    notification = notify_corporate_event_scan(
        result,
        webhook=os.getenv("FEISHU_WEBHOOK_URL", "").strip(),
        dry_run=args.dry_run,
    )
    print(f"[corporate_event_scan] hits={len(result.hits)} markdown={result.markdown_path}")
    print(f"[corporate_event_scan] json={result.json_path}")
    print(f"[corporate_event_scan] notification: {notification.reason}")
    if not result.source_ok:
        return 2
    if not args.dry_run and not notification.ok:
        return 1
    return 0


def _load_extra(path: str) -> list | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else None


if __name__ == "__main__":
    raise SystemExit(main())
