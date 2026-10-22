"""Date-keyed native turnover for controlled snapshot research."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

from integrations.tushare_client import get_pro
from workflows.funnel_data_quality import TURNOVER_MIN_COVERAGE

TURNOVER_CONTRACT = "date_keyed_native_pct_v1"


def attach_snapshot_pit_turnover(frames: list[pd.DataFrame]) -> dict:
    pro = get_pro()
    if pro is None:
        raise RuntimeError("历史换手取数需要已配置Tushare，禁止用当前股本或0替代")
    days = sorted({str(day) for frame in frames for day in pd.to_datetime(frame["date"]).dt.strftime("%Y%m%d")})
    lookup: dict[tuple[str, str], float] = {}
    for day in days:
        raw = pro.daily_basic(trade_date=day, fields="ts_code,trade_date,turnover_rate")
        if raw is None or raw.empty:
            continue
        if not {"ts_code", "trade_date", "turnover_rate"}.issubset(raw.columns):
            raise ValueError("daily_basic缺少历史换手日期键或原生值")
        for row in raw.itertuples(index=False):
            key = (str(row.ts_code).split(".")[0], str(row.trade_date))
            if key[1] != day:
                raise ValueError("daily_basic返回非请求日的数据，拒绝串日")
            value = pd.to_numeric(row.turnover_rate, errors="coerce")
            if pd.notna(value) and math.isfinite(value) and value >= 0:
                lookup[key] = float(value)
    for frame in frames:
        values = pd.to_numeric(frame.get("turnover", pd.Series(index=frame.index, dtype=float)), errors="coerce")
        values = values.where(values.ge(0) & values.map(lambda value: pd.notna(value) and math.isfinite(value)))
        dates = pd.to_datetime(frame["date"]).dt.strftime("%Y%m%d")
        derived = pd.Series(
            [lookup.get((str(code).zfill(6), day)) for code, day in zip(frame["symbol"], dates, strict=True)],
            index=frame.index,
            dtype=float,
        )
        frame["turnover_source"] = "native"
        frame.loc[values.isna(), "turnover_source"] = "tushare.daily_basic"
        frame["turnover"] = values.fillna(derived)
        frame.loc[frame["turnover"].isna(), "turnover_source"] = "missing"
    return {"contract": TURNOVER_CONTRACT, "days_requested": len(days), "valid_native_keys": len(lookup)}


def validate_snapshot_turnover(df_map: dict[str, pd.DataFrame], metadata: dict, start: date, end: date) -> dict:
    if (metadata.get("turnover_pit") or {}).get("contract") != TURNOVER_CONTRACT:
        raise ValueError("严格消融拒绝旧快照：缺少日期键原生换手合同，须重新取数")
    pieces = []
    for frame in df_map.values():
        if "turnover" not in frame.columns:
            raise ValueError("严格消融缺少turnover列，禁止静默旁路")
        dates = pd.to_datetime(frame["date"]).dt.date
        values = pd.to_numeric(frame["turnover"], errors="coerce")
        valid = values.ge(0) & values.map(lambda value: pd.notna(value) and math.isfinite(value))
        pieces.append(pd.DataFrame({"date": dates, "valid": valid}).loc[dates.between(start, end)])
    rows = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    if rows.empty:
        raise ValueError("严格消融区间无行情行，不能生成收益结论")
    coverage = rows.groupby("date")["valid"].mean()
    failed = coverage[coverage < TURNOVER_MIN_COVERAGE]
    if not failed.empty:
        raise ValueError(f"严格消融换手覆盖不足95%：{dict(failed.head(10))}；缺失不填0")
    return {"days": len(coverage), "min_coverage": float(coverage.min()), "rows": len(rows)}
