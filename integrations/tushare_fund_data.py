"""Tushare fund-history fallback for ETF/LOF enhancement."""

from __future__ import annotations

import pandas as pd

from core.wyckoff_engine import normalize_hist_from_fetch
from integrations.data_source_format import to_ts_code
from integrations.tushare_client import get_pro


def fetch_fund_daily(symbol: str, start, end) -> pd.DataFrame | None:
    pro = get_pro()
    if pro is None:
        return None
    frame = pro.fund_daily(
        ts_code=to_ts_code(symbol),
        start_date=_ymd(start),
        end_date=_ymd(end),
    )
    if frame is None or frame.empty:
        return None
    out = frame.rename(
        columns={
            "trade_date": "date",
            "vol": "volume",
        }
    ).copy()
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce") * 1000
    dates = out["date"].astype(str)
    out["date"] = dates.str[:4] + "-" + dates.str[4:6] + "-" + dates.str[6:8]
    normalized = normalize_hist_from_fetch(out)
    normalized.attrs["source"] = "tushare_fund_daily"
    normalized.attrs["upstream_source"] = "tushare_fund_daily"
    return normalized.sort_values("date").reset_index(drop=True)


def _ymd(value) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")
