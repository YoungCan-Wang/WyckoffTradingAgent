"""赛马 T3 板块强度组合回放（研究脚本）。

因子复用 scripts/scan_factor_ic.py 的 sector_strength_5d（本仓唯一正 IC 维度）。
规格：信号日 sector_strength_5d 截面 top20% 的股票，T+1 开盘买、持有 5 日收盘出。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from core.trade_friction import round_trip_cost_pct
from scripts.research_t2_oversold import benchmark_switch, build_panels, stats

COST = round_trip_cost_pct()


def build_trades(panels: dict, cache: str, *, hold: int = 5, top_pct: float = 0.8) -> pd.DataFrame:
    close, open_ = panels["close"], panels["open"]
    strength, amt = panels["factors"]["sector_strength_5d"], panels["factors"]["turnover_amt"]
    switch = benchmark_switch(cache, list(close.index))
    dates = list(close.index)
    rows: list[dict[str, Any]] = []
    for i in range(60, len(dates) - hold - 1):
        day = dates[i]
        if not bool(switch.iloc[i]):
            continue
        s, a = strength.loc[day].dropna(), amt.loc[day]
        liquid = a[a >= a.quantile(0.5)].index
        s = s[s.index.isin(liquid)]
        if len(s) < 20:
            continue
        picks = s[s >= s.quantile(top_pct)].index
        for code in picks:
            entry_day, entry_px = dates[i + 1], open_.loc[dates[i + 1], code]
            exit_px = close.loc[dates[i + 1 + hold], code]
            if pd.isna(entry_px) or pd.isna(exit_px) or entry_px <= 0:
                continue
            rows.append(
                {
                    "signal_date": str(day.date()),
                    "code": str(code),
                    "entry_date": str(entry_day.date()),
                    "exit_date": str(dates[i + 1 + hold].date()),
                    "ret_pct": round((exit_px / entry_px - 1) * 100 - COST, 4),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        daily = close.pct_change(fill_method=None).mean(axis=1)
        cum = (1 + daily.fillna(0)).cumprod()
        base = {str(d.date()): v for d, v in zip(cum.index, cum)}
        df["excess_pct"] = [
            round(r - (base[e] / base[s] - 1) * 100, 4)
            for r, s, e in zip(df["ret_pct"] + COST, df["signal_date"], df["exit_date"])
            if s in base and e in base
        ]
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="T3 板块强度组合回放")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--start", default="2025-01-02")
    parser.add_argument("--out", default="tmp/t3_result.json")
    args = parser.parse_args()
    trades = build_trades(build_panels(args.cache, args.start), args.cache)
    result: dict[str, Any] = {"signal_level": stats(trades)}
    if not trades.empty and "excess_pct" in trades.columns:
        ex = trades["excess_pct"].dropna()
        result["excess_vs_equal_weight"] = {
            "n": int(len(ex)),
            "mean": round(float(ex.mean()), 4),
            "median": round(float(ex.median()), 4),
            "pct_positive": round(float((ex > 0).mean() * 100), 2),
        }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
