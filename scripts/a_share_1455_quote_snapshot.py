"""Strict, read-only first quote snapshot for the independent 14:55 A-share study."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from integrations.tickflow_client import TICKFLOW_BASE_URL

_BEIJING = ZoneInfo("Asia/Shanghai")
_UNIVERSE = "CN_Equity_A"
_SYMBOL = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_SAFE_ERROR = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
_MIN_COVERAGE = 5200
_PAGE_SIZE = 20


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _quote_time(value: Any) -> datetime | None:
    stamp = _number(value)
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp / (1000 if stamp > 10_000_000_000 else 1), tz=UTC).astimezone(_BEIJING)
    except (OverflowError, OSError, ValueError):
        return None


def _error_code(response: requests.Response) -> str | None:
    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and _SAFE_ERROR.fullmatch(code) else None


def _base_result(now: datetime) -> dict[str, Any]:
    return {
        "ok": False,
        "source": "TickFlow GET /v1/quotes?universes=CN_Equity_A",
        "local_time": now.isoformat(timespec="seconds"),
        "coverage": 0,
        "valid_quote_count": 0,
        "duplicate_count": 0,
        "null_count": 0,
        "candidate_count": 0,
        "candidate_count_basis": "provisional_price_screen_pending_corporate_action_check",
        "industry_counts": {"unmapped": 0},
        "candidates": [],
    }


def _parse_quotes(rows: list[Any], now: datetime, result: dict[str, Any], min_coverage: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    active: list[tuple[float, datetime]] = []
    null_count = duplicate_count = mismatch_count = valid_quote_count = 0
    for row in rows:
        if not isinstance(row, dict):
            null_count += 1
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            null_count += 1
            continue
        ext = row.get("ext") if isinstance(row.get("ext"), dict) else {}
        if ext.get("type") not in (None, "cn_equity") or row.get("region") not in (None, "CN"):
            null_count += 1
            continue
        if symbol in seen:
            duplicate_count += 1
            continue
        seen.add(symbol)
        price, prev_close = _number(row.get("last_price")), _number(row.get("prev_close"))
        traded_at = _quote_time(row.get("timestamp"))
        amount = _number(row.get("amount"))
        if price is None or price <= 0 or prev_close is None or prev_close <= 0 or traded_at is None:
            null_count += 1
            continue
        if amount is not None and amount > 0:
            active.append((amount, traded_at))
        name = str(ext.get("name") or "").upper()
        if not name:
            null_count += 1
            continue
        valid_quote_count += 1
        if "ST" in name or "退" in name:
            continue
        pct = (price / prev_close - 1) * 100
        if pct > -7:
            continue
        vendor_pct = _number(ext.get("change_pct"))
        pct_mismatch = vendor_pct is not None and abs(vendor_pct - pct) > 0.5
        mismatch_count += int(pct_mismatch)
        age_seconds = round((now - traded_at).total_seconds())
        candidates.append(
            {
                "symbol": symbol,
                "price": price,
                "pct": round(pct, 3),
                "amount": amount,
                "timestamp": traded_at.isoformat(timespec="seconds"),
                "age_seconds": age_seconds,
                "needs_recheck": age_seconds > 60 or age_seconds < -60 or pct_mismatch,
            }
        )
    candidates.sort(key=lambda item: (item["pct"], item["symbol"]))
    result.update(
        coverage=len(seen),
        valid_quote_count=valid_quote_count,
        duplicate_count=duplicate_count,
        null_count=null_count,
        candidate_count=len(candidates),
        quote_pct_mismatch_count=mismatch_count,
        industry_counts={"unmapped": len(candidates)},
        candidates=[
            {key: candidate[key] for key in ("symbol", "price", "pct", "amount", "timestamp")}
            for candidate in candidates[:_PAGE_SIZE]
        ],
    )
    if valid_quote_count < min_coverage or duplicate_count or null_count > max(10, len(rows) // 100):
        result["failure"] = "DATA_INCOMPLETE"
        return candidates
    if not active:
        result["failure"] = "NO_ACTIVE_QUOTES"
        return candidates
    newest = max((traded_at for _, traded_at in sorted(active, reverse=True)[:20]), default=None)
    result["active_latest_time"] = newest.isoformat(timespec="seconds") if newest else None
    if newest is None or newest.date() != now.date():
        result["failure"] = "CORE_QUOTES_STALE"
    elif now.time() >= time(15) and newest.time() >= time(14, 57):
        result["market_state"] = "closed_next_day_observation_only"
        result["ok"] = True
    elif (now - newest).total_seconds() > 60 or (newest - now).total_seconds() > 60:
        result["failure"] = "CORE_QUOTES_STALE"
    else:
        result["market_state"] = "live"
        result["ok"] = True
    return candidates


def fetch_snapshot(
    api_key: str,
    *,
    now: datetime | None = None,
    session_factory: type[requests.Session] = requests.Session,
    min_coverage: int = _MIN_COVERAGE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch one full-A response without proxy inheritance or stale-cache fallback."""
    observed = (now or datetime.now(_BEIJING)).astimezone(_BEIJING)
    result = _base_result(observed)
    session = session_factory()
    session.trust_env = False
    try:
        response = session.get(
            f"{TICKFLOW_BASE_URL}/v1/quotes",
            params={"universes": _UNIVERSE},
            headers={"x-api-key": api_key},
            timeout=(3, 12),
            allow_redirects=False,
        )
        received = datetime.now(_BEIJING)
        result.update(
            http_status=response.status_code,
            response_bytes=len(response.content),
            cf_ray=response.headers.get("CF-RAY"),
            server_time=response.headers.get("Date"),
            received_time=received.isoformat(timespec="seconds"),
        )
        if response.status_code != 200:
            result.update(failure="HTTP_ERROR", error_code=_error_code(response))
            return result, []
        try:
            payload = response.json()
        except ValueError:
            result["failure"] = "INVALID_JSON"
            return result, []
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            result["failure"] = "INVALID_PAYLOAD"
            return result, []
        return result, _parse_quotes(rows, observed if now is not None else received, result, min_coverage)
    except requests.RequestException as exc:
        result.update(failure="NETWORK_ERROR", exception_type=type(exc).__name__)
        return result, []
    finally:
        session.close()


def _write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true", help="Page through the in-memory candidate pool")
    args = parser.parse_args()
    from cli.auth import load_config

    try:
        key = os.getenv("TICKFLOW_API_KEY") or str(load_config().get("tickflow_api_key") or "")
    except Exception:
        result = _base_result(datetime.now(_BEIJING))
        result["failure"] = "LOCAL_AUTH_UNAVAILABLE"
        _write_json(result)
        return 1
    if not key.strip():
        result = _base_result(datetime.now(_BEIJING))
        result["failure"] = "LOCAL_AUTH_MISSING"
        _write_json(result)
        return 1
    result, candidates = fetch_snapshot(key.strip())
    _write_json(result)
    if not result["ok"] or not args.interactive:
        return 0 if result["ok"] else 1
    for raw in sys.stdin:
        command = raw.strip().split()
        if command == ["quit"]:
            break
        if len(command) != 2 or command[0] != "page" or not command[1].isdigit():
            _write_json({"error": "USE_PAGE_NUMBER_OR_QUIT"})
            continue
        page = int(command[1])
        start = page * _PAGE_SIZE
        _write_json({"page": page, "candidates": candidates[start : start + _PAGE_SIZE]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
