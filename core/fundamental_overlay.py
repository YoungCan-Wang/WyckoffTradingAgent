"""Deterministic fundamental-quality overlay for research and shadow evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

FUNDAMENTAL_OVERLAY_SCHEMA_VERSION = "fundamental-overlay-v1"
CORE_FIELDS = (
    "roe",
    "net_income_yoy",
    "revenue_yoy",
    "gross_margin",
    "debt_to_asset_ratio",
    "operating_cash_to_revenue",
)
FIELD_ALIASES = {
    "roe": ("roe", "roe_diluted"),
    "net_income_yoy": ("net_income_yoy", "netprofit_yoy"),
    "revenue_yoy": ("revenue_yoy", "or_yoy"),
    "gross_margin": ("gross_margin", "grossprofit_margin"),
    "debt_to_asset_ratio": ("debt_to_asset_ratio", "debt_to_assets"),
    "operating_cash_to_revenue": ("operating_cash_to_revenue", "ocf_to_or"),
}


def evaluate_fundamental_overlay(
    metrics: Mapping[str, Any] | None,
    *,
    signal_date: str | date,
    max_report_age_days: int = 550,
) -> dict[str, Any]:
    """Score only information that was already public before ``signal_date``."""
    values = {field: _metric(metrics, field) for field in CORE_FIELDS}
    available = sum(value is not None for value in values.values())
    report_age = _report_age_days(metrics, signal_date)
    stale = report_age is not None and report_age > max_report_age_days
    if not metrics or available < 3 or stale:
        return _result(values, available, report_age, "unknown", "observe", 0, [], [])

    score, positives, negatives = _score(values)
    severe = _severe_risk(values)
    grade = "strong" if score >= 3 else "weak" if score < 0 else "neutral"
    action = "veto" if severe else "cap" if grade == "weak" else "boost" if grade == "strong" else "observe"
    return _result(values, available, report_age, grade, action, score, positives, negatives)


def _score(values: dict[str, float | None]) -> tuple[int, list[str], list[str]]:
    score = 0
    positives: list[str] = []
    negatives: list[str] = []
    score += _rule(
        values["roe"], high=10, low=0, points=(2, -2), codes=("ROE_STRONG", "ROE_LOSS"), out=(positives, negatives)
    )
    score += _rule(
        values["net_income_yoy"],
        high=0,
        low=0,
        points=(1, -1),
        codes=("PROFIT_GROWTH", "PROFIT_DECLINE"),
        out=(positives, negatives),
        strict=True,
    )
    score += _rule(
        values["revenue_yoy"],
        high=0,
        low=0,
        points=(1, -1),
        codes=("REVENUE_GROWTH", "REVENUE_DECLINE"),
        out=(positives, negatives),
        strict=True,
    )
    score += _rule(
        values["gross_margin"],
        high=30,
        low=15,
        points=(1, -1),
        codes=("MARGIN_HIGH", "MARGIN_LOW"),
        out=(positives, negatives),
    )
    score += _inverse_rule(values["debt_to_asset_ratio"], good=55, bad=70, out=(positives, negatives))
    score += _rule(
        values["operating_cash_to_revenue"],
        high=5,
        low=0,
        points=(1, -1),
        codes=("CASH_FLOW_HEALTHY", "CASH_FLOW_NEGATIVE"),
        out=(positives, negatives),
    )
    return score, positives, negatives


def _rule(value, *, high, low, points, codes, out, strict: bool = False) -> int:
    if value is None:
        return 0
    high_match = value > high if strict else value >= high
    low_match = value < low
    if high_match:
        out[0].append(codes[0])
        return points[0]
    if low_match:
        out[1].append(codes[1])
        return points[1]
    return 0


def _inverse_rule(value: float | None, *, good: float, bad: float, out) -> int:
    if value is None:
        return 0
    if value <= good:
        out[0].append("LEVERAGE_MODERATE")
        return 1
    if value >= bad:
        out[1].append("LEVERAGE_HIGH")
        return -2
    return 0


def _severe_risk(values: dict[str, float | None]) -> bool:
    distress = sum(
        (
            _below(values["roe"], 0),
            _below(values["net_income_yoy"], -30),
            _below(values["revenue_yoy"], -20),
            _below(values["operating_cash_to_revenue"], 0),
        )
    )
    leveraged_loss = _above(values["debt_to_asset_ratio"], 85) and _below(values["roe"], 0)
    return distress >= 3 or leveraged_loss


def _result(values, available, report_age, grade, action, score, positives, negatives) -> dict[str, Any]:
    return {
        "schema_version": FUNDAMENTAL_OVERLAY_SCHEMA_VERSION,
        "grade": grade,
        "action": action,
        "score": score,
        "confidence_delta": 1 if action == "boost" else -1 if action in {"cap", "veto"} else 0,
        "position_cap": 0.0 if action == "veto" else 0.5 if action == "cap" else 1.0,
        "available_fields": available,
        "report_age_days": report_age,
        "positive_rules": positives,
        "negative_rules": negatives,
        "metrics": values,
    }


def _metric(metrics: Mapping[str, Any] | None, field: str) -> float | None:
    if not metrics:
        return None
    for alias in FIELD_ALIASES[field]:
        try:
            value = float(metrics.get(alias))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _report_age_days(metrics: Mapping[str, Any] | None, signal_date: str | date) -> int | None:
    if not metrics:
        return None
    period_end = _as_date(metrics.get("period_end") or metrics.get("end_date"))
    signal = _as_date(signal_date)
    return (signal - period_end).days if signal and period_end and signal >= period_end else None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _below(value: float | None, threshold: float) -> bool:
    return value is not None and value < threshold


def _above(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold
