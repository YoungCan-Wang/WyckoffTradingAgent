"""Research-only Signal → Event → Position view of existing L4 types."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SIGNAL_CATALOG = {
    "spring": {"family": "accum", "event": "test_supply"},
    "lps": {"family": "accum", "event": "last_point_support"},
    "compression": {"family": "accum", "event": "coil"},
    "sos": {"family": "trend", "event": "sign_of_strength"},
    "evr": {"family": "trend", "event": "effort_vs_result"},
    "trend_pullback": {"family": "trend", "event": "pullback"},
    "utad": {"family": "distrib", "event": "upthrust_after_distribution"},
    "upthrust": {"family": "distrib", "event": "upthrust"},
}


def to_signal(signal_type: str, code: str, trade_date: str, **fields: Any) -> dict[str, Any]:
    key = str(signal_type or "").strip().lower()
    meta = SIGNAL_CATALOG.get(key, {"family": "unknown", "event": key or "unknown"})
    return {
        "signal_type": key,
        "code": str(code),
        "trade_date": str(trade_date),
        "family": meta["family"],
        "event": meta["event"],
        **fields,
    }


def event_matches(
    signals: Iterable[dict[str, Any]],
    *,
    all_of: Iterable[str] = (),
    any_of: Iterable[str] = (),
    not_of: Iterable[str] = (),
) -> bool:
    present = {str(item.get("signal_type") or "") for item in signals}
    required = {str(item) for item in all_of}
    optional = {str(item) for item in any_of}
    forbidden = {str(item) for item in not_of}
    if required and not required.issubset(present):
        return False
    if optional and present.isdisjoint(optional):
        return False
    if forbidden and not present.isdisjoint(forbidden):
        return False
    return True


def position_intent(matched: bool, family: str) -> str:
    if not matched:
        return "FLAT"
    if family == "distrib":
        return "EXIT"
    if family == "accum":
        return "PROBE"
    if family == "trend":
        return "HOLD"
    return "FLAT"
