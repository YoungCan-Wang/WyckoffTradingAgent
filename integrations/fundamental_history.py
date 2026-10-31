"""Point-in-time financial history from TickFlow, keyed on the repository's six-digit code.

``/v1/financials/metrics?latest=false`` returns every quarterly record since listing with an
``announce_date``; a record may only be used on signal dates strictly after that day, since
the vendor gives a date without a time. Records without an announce date are dropped rather
than approximated by ``period_end`` — the report-period end is not when the market learned it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from core.asof_cut import FINANCIAL_RECORD_MAX_AGE_DAYS
from core.candidate_metadata import code6

HISTORY_FIELDS = (
    "announce_date",
    "period_end",
    "roe",
    "roe_diluted",
    "debt_to_asset_ratio",
    "operating_cash_to_revenue",
    "gross_margin",
    "net_income_yoy",
    "revenue_yoy",
)


def fetch_financial_history(api_key: str, symbols: list[str], *, since: date) -> dict[str, list[dict[str, Any]]]:
    """Quarterly records announced on or after ``since - FINANCIAL_RECORD_MAX_AGE_DAYS``, trimmed to HISTORY_FIELDS.

    The full vendor payload for the A-share universe is ~130 MB; the backtest only ever looks
    back one staleness window before its first signal date, so older rows are not written.
    """
    from integrations.tickflow_client import TickFlowClient

    floor = (since - timedelta(days=FINANCIAL_RECORD_MAX_AGE_DAYS)).isoformat()
    raw = TickFlowClient(api_key=api_key).get_financial_metrics(symbols, latest=False)
    out: dict[str, list[dict[str, Any]]] = {}
    for sym, records in raw.items():
        code = code6(sym)
        kept = [
            {key: item.get(key) for key in HISTORY_FIELDS}
            for item in records
            if isinstance(item, dict) and str(item.get("announce_date") or "") >= floor
        ]
        if code and kept:
            out[code] = sorted(kept, key=lambda item: (str(item["announce_date"]), str(item.get("period_end") or "")))
    return out
