"""Read-only, date-based Tushare snapshots; never selects today's surviving stocks."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

FIELDS = {
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "stk_limit": "ts_code,trade_date,up_limit,down_limit",
}
CAPS = {"daily": 6000, "adj_factor": 6000, "stk_limit": 5800}
LOCK = threading.Lock()
LAST_CALL = 0.0


def throttle() -> None:
    global LAST_CALL
    with LOCK:
        time.sleep(max(0, LAST_CALL + 0.21 - time.monotonic()))
        LAST_CALL = time.monotonic()


def query(api: str, params: dict, fields: str) -> dict:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TUSHARE_TOKEN missing")
    request = urllib.request.Request(
        "https://api.tushare.pro",
        data=json.dumps({"api_name": api, "token": token, "params": params, "fields": fields}).encode(),
        headers={"Content-Type": "application/json"},
    )
    error = "unknown"
    for attempt in range(4):
        throttle()
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.load(response)
            if payload.get("code") != 0:
                raise RuntimeError(f"API status {payload.get('code')}")
            data = payload.get("data") or {}
            if not data.get("items"):
                raise ValueError("empty response")
            return data
        except Exception as exc:
            error = type(exc).__name__
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{api}: {error}; no data substituted")


def collect_day(day: str, root: Path) -> dict:
    target = root / f"{day}.json.gz"
    if target.exists():
        raw = gzip.decompress(target.read_bytes())
        payload = json.loads(raw)
    else:
        payload = {api: query(api, {"trade_date": day}, fields) for api, fields in FIELDS.items()}
        for api, data in payload.items():
            if len(data["items"]) >= CAPS[api]:
                raise RuntimeError(f"{day} {api}: response at row cap; reject possible truncation")
            columns = data["fields"]
            codes = [row[columns.index("ts_code")] for row in data["items"]]
            dates = {str(row[columns.index("trade_date")]) for row in data["items"]}
            if len(codes) != len(set(codes)) or dates != {day}:
                raise RuntimeError(f"{day} {api}: duplicate code or incorrect date")
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(gzip.compress(raw, compresslevel=6, mtime=0))
        temporary.replace(target)
    return {"date": day, "sha256": hashlib.sha256(raw).hexdigest(),
            **{api: len(data["items"]) for api, data in payload.items()}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20200901")
    parser.add_argument("--end", default="20260918")
    parser.add_argument("--out", default="artifacts/fib-audit-data")
    args = parser.parse_args()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    calendar = query("trade_cal", {"exchange": "SSE", "start_date": args.start,
                                   "end_date": args.end, "is_open": "1"}, "cal_date,is_open")
    idx = calendar["fields"].index("cal_date")
    days = sorted(str(row[idx]) for row in calendar["items"])
    if len(days) != len(set(days)):
        raise RuntimeError("duplicate calendar dates")
    (root / "calendar.json").write_text(json.dumps(days))
    protocol = {"source": "Tushare Pro", "start": args.start, "end": args.end,
                "code_sha": os.environ.get("GITHUB_SHA"), "days": len(days),
                "fields": FIELDS, "selection": "historical date cross-sections, no current-stock filter",
                "historical_vintage_available": False, "status": "collecting", "files": [], "errors": []}
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(protocol, indent=2))
    print(f"Collecting {len(days)} dates; immutable snapshot; at most 286 requests/minute", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(collect_day, day, root): day for day in days}
        for n, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                protocol["files"].append(future.result())
            except Exception as exc:
                protocol["errors"].append({"date": futures[future], "error": str(exc)})
            if n % 50 == 0 or n == len(days):
                print(f"Completed {n}/{len(days)}; errors={len(protocol['errors'])}", flush=True)
                manifest.write_text(json.dumps(protocol, indent=2))
    protocol["files"].sort(key=lambda row: row["date"])
    protocol["status"] = "failed" if protocol["errors"] else "complete"
    protocol["snapshot_hash"] = hashlib.sha256(json.dumps(protocol["files"], sort_keys=True).encode()).hexdigest()
    manifest.write_text(json.dumps(protocol, indent=2))
    print(json.dumps({key: value for key, value in protocol.items() if key != "files"}, indent=2))
    if protocol["errors"]:
        raise SystemExit("Incomplete collection; cannot claim a complete-period backtest")


if __name__ == "__main__":
    main()
