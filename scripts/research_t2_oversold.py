"""赛马 T2 超跌反弹组合回放（研究脚本，口径见 Issue #587）。

复用 scripts/scan_factor_ic.py 的因子面板（与生产同名因子），不手搓因子定义。
输入为 backtest 快照的 hist_full.csv.gz；信号级与资金级（最多 10 仓等权）双口径。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import pandas as pd

from core.trade_friction import round_trip_cost_pct

COST = round_trip_cost_pct()
ROOT = Path(__file__).resolve().parents[1]


def _load_factor_module() -> Any:
    spec = importlib.util.spec_from_file_location("scan_factor_ic", ROOT / "scripts" / "scan_factor_ic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_panels(cache: str, start: str) -> dict[str, pd.DataFrame]:
    mod = _load_factor_module()
    market = mod.load_market(cache, start)
    factors, close, open_ = mod.build_factors(market)
    piv = {c: market.pivot_table(index="d", columns="ts_code", values=c) for c in ("high", "low", "vol", "amount")}
    return {"factors": factors, "close": close, "open": open_, **piv}


def market_switch(close: pd.DataFrame) -> pd.Series:
    """等权市场指数站上自身 60 日线才开仓（与 T4 同一开关的面板内实现）。"""
    daily = close.pct_change(fill_method=None).mean(axis=1)
    index = (1 + daily.fillna(0)).cumprod()
    return index > index.rolling(60).mean()


def benchmark_switch(cache: str, dates: list) -> pd.Series:
    """上证基准（快照 benchmark_main）站上 60 日线才开仓。"""
    bench = pd.read_csv(Path(cache).parent / "benchmark_main.csv")
    bench["d"] = pd.to_datetime(bench["date"])
    bench = bench.sort_values("d")
    on = (bench["close"] > bench["close"].rolling(60).mean()).to_numpy()
    lut = {d: bool(v) for d, v in zip(bench["d"], on)}
    return pd.Series([lut.get(pd.Timestamp(d), False) for d in dates], index=dates)


def build_trades(
    panels: dict[str, pd.DataFrame],
    *,
    hold: int = 10,
    stop: float = 8.0,
    quantile: float = 0.05,
    switch: pd.Series | None = None,
) -> pd.DataFrame:
    close, open_, low = panels["close"], panels["open"], panels["low"]
    ret20, amt = panels["factors"]["ret20"], panels["factors"]["turnover_amt"]
    ma20 = close.rolling(20).mean()
    if switch is None:
        switch = market_switch(close)
    dates = list(close.index)
    rows: list[dict[str, Any]] = []
    for i in range(60, len(dates) - hold - 1):
        day = dates[i]
        if not bool(switch.iloc[i]):
            continue
        r, a = ret20.loc[day].dropna(), amt.loc[day]
        liquid = a[a >= a.quantile(0.5)].index
        r = r[r.index.isin(liquid)]
        if len(r) < 20:
            continue
        picks = r.nsmallest(max(int(len(r) * quantile), 1)).index
        for code in picks:
            c, o = close.loc[day, code], open_.loc[day, code]
            if not (c >= ma20.loc[day, code] * 0.97 or c > o):
                continue
            entry_day, entry_px = dates[i + 1], open_.loc[dates[i + 1], code]
            if pd.isna(entry_px) or entry_px <= 0:
                continue
            exit_px, reason = close.loc[dates[i + 1 + hold], code], "hold"
            exit_day = dates[i + 1 + hold]
            for j in range(i + 1, i + 2 + hold):
                if stop > 0 and low.loc[dates[j], code] <= entry_px * (1 - stop / 100):
                    exit_px, reason, exit_day = entry_px * (1 - stop / 100), "stop", dates[j]
                    break
            if pd.isna(exit_px) or exit_px <= 0:
                continue
            rows.append(
                {
                    "signal_date": str(day.date()),
                    "code": str(code),
                    "entry_date": str(entry_day.date()),
                    "exit_date": str(exit_day.date()),
                    "ret_pct": round((exit_px / entry_px - 1) * 100 - COST, 4),
                    "exit_reason": reason,
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        market_daily = close.pct_change(fill_method=None).mean(axis=1)
        market_cum = (1 + market_daily.fillna(0)).cumprod()
        base = {str(d.date()): v for d, v in zip(market_cum.index, market_cum)}
        df["excess_pct"] = [
            round(r - (base[e] / base[s] - 1) * 100, 4)
            for r, s, e in zip(df["ret_pct"] + COST, df["signal_date"], df["exit_date"])
            if s in base and e in base
        ]
    return df


def stats(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"n": 0}
    r = df["ret_pct"]
    return {
        "n": int(len(r)),
        "win_rate": round(float((r > 0).mean() * 100), 2),
        "mean": round(float(r.mean()), 4),
        "median": round(float(r.median()), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="T2 超跌反弹组合回放")
    parser.add_argument("--cache", required=True, help="backtest 快照 hist_full.csv.gz 路径")
    parser.add_argument("--start", default="2025-01-02")
    parser.add_argument("--out", default="tmp/t2_result.json")
    parser.add_argument("--stop", type=float, default=8.0, help="止损百分比，0=不设止损")
    parser.add_argument("--hold", type=int, default=10)
    parser.add_argument("--switch", choices=["equal_weight", "benchmark"], default="equal_weight")
    args = parser.parse_args()
    panels = build_panels(args.cache, args.start)
    switch = benchmark_switch(args.cache, list(panels["close"].index)) if args.switch == "benchmark" else None
    trades = build_trades(panels, hold=args.hold, stop=args.stop, switch=switch)
    result = {"cost_pct": COST, "signal_level": stats(trades), "by_exit": {}}
    if not trades.empty and "excess_pct" in trades.columns:
        ex = trades["excess_pct"].dropna()
        result["excess_vs_equal_weight"] = {
            "n": int(len(ex)),
            "mean": round(float(ex.mean()), 4),
            "median": round(float(ex.median()), 4),
            "pct_positive": round(float((ex > 0).mean() * 100), 2),
        }
    if not trades.empty:
        result["by_exit"] = {k: stats(g) for k, g in trades.groupby("exit_reason")}
        result["by_month"] = {k: stats(g) for k, g in trades.groupby(trades["signal_date"].str[:7])}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
