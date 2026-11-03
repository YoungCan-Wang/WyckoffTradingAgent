"""Recommendation-tracking persistence for HK/US market funnel jobs."""

from __future__ import annotations

from typing import Any

from utils.env import env_flag


def write_tracking_candidates_if_enabled(candidates: list[dict[str, Any]], market: str) -> None:
    if env_flag("MARKET_FUNNEL_WRITE_DB"):
        upsert_funnel_to_tracking(candidates, market)


def upsert_funnel_to_tracking(candidates: list[dict[str, Any]], market: str) -> None:
    if not candidates or market not in ("us", "hk"):
        return
    from integrations.recommendation_global import upsert_global_recommendations

    rows_by_date: dict[int, list[dict[str, Any]]] = {}
    skipped = 0
    for candidate in candidates:
        recommend_date = _candidate_trade_date(candidate)
        if recommend_date is None:
            skipped += 1
            continue
        rows_by_date.setdefault(recommend_date, []).append(tracking_row(candidate))
    if not rows_by_date:
        raise ValueError("cannot resolve recommendation trade date from market histories")
    for recommend_date, rows in sorted(rows_by_date.items()):
        ok = upsert_global_recommendations(recommend_date, rows, market)
        print(f"[market-funnel] DB write: market={market}, date={recommend_date}, candidates={len(rows)}, ok={ok}")
        if not ok:
            raise RuntimeError(f"DB write failed for market={market}, candidates={len(rows)}")
    if skipped:
        print(f"[market-funnel] DB write skipped candidates without trade date: {skipped}/{len(candidates)}")


def _candidate_trade_date(candidate: dict[str, Any]) -> int | None:
    try:
        date_int = int(candidate.get("latest_trade_date"))
    except (TypeError, ValueError):
        return None
    return date_int if 19000101 <= date_int <= 29991231 else None


_SPRINGBOARD_KEYS = (
    "springboard_a",
    "springboard_b",
    "springboard_c",
    "springboard_combo",
    "springboard_grade",
    "springboard_met_count",
    "springboard_support",
    "springboard_touch_count",
    "springboard_evidence",
    "springboard_scored",
)
_ENTRY_TIER_KEYS = (
    "actionable",
    "entry_quality_grade",
    "entry_quality_score",
    "bias_20",
    "bias_200",
    "support_level",
)


def tracking_row(candidate: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": str(candidate.get("symbol", "")).strip(),
        "name": str(candidate.get("name", "")).strip(),
        "tag": ",".join(candidate.get("triggers") or []),
        "score": float(candidate.get("score") or 0),
        "latest_close": float(candidate.get("latest_close") or 0),
    }
    if candidate.get("springboard_scored"):
        for key in _SPRINGBOARD_KEYS:
            if key in candidate:
                row[key] = candidate[key]
    for key in _ENTRY_TIER_KEYS:
        if key in candidate:
            row[key] = candidate[key]
    return row
