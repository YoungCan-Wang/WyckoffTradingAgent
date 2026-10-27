"""赛马 T4：中证1000 ETF MA 择时可交易口径回测（规格见 Issue #592）。

信号：收盘站上 MA 次日开盘买入、跌破次日开盘清仓；每次切换扣总资金 0.10%。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

SWITCH_COST = 0.0010
SEGMENTS = [("2017", "2019"), ("2020", "2022"), ("2023", "2024"), ("2025", "2026")]


def load_etf(symbol: str, count: int = 2500) -> pd.DataFrame:
    from integrations.tickflow_client import TickFlowClient

    cli = TickFlowClient(api_key=os.getenv("TICKFLOW_API_KEY", ""))
    df = cli.get_klines(symbol, period="1d", count=count)
    return df[["date", "open", "close"]].copy()


def run_timing(df: pd.DataFrame, ma_window: int, *, breaker: bool = False, dual: bool = False) -> dict[str, Any]:
    df = df.copy()
    df["ma"] = df["close"].rolling(ma_window).mean()
    df["ma_fast"] = df["close"].rolling(20).mean()
    df = df.dropna().reset_index(drop=True)
    cash, shares, peak, halved = 1.0, 0.0, 1.0, False
    equity: list[float] = []
    switches = 0
    invested_days = 0
    for i in range(len(df) - 1):
        signal_long = float(df["close"].iloc[i]) > float(df["ma"].iloc[i])
        if dual:
            signal_long = signal_long and float(df["ma_fast"].iloc[i]) > float(df["ma"].iloc[i])
        nav = cash + shares * float(df["close"].iloc[i])
        peak = max(peak, nav)
        if breaker and i % 5 == 0:
            # 按周检查 + 迟滞：回撤 ≥12% 减半，回到高点 -6% 内才恢复，防阈值附近震荡换手。
            dd = nav / peak - 1
            halved = dd <= -0.12 if not halved else dd < -0.06
        target = (0.5 if halved else 1.0) if signal_long else 0.0
        current = (shares * float(df["close"].iloc[i]) / nav) if nav > 0 else 0.0
        # 半仓是状态而非每日再平衡目标：只在满/半/空三态切换时调仓，避免权重漂移天天换手。
        state = 0 if target == 0 else (1 if target == 0.5 else 2)
        prev_state = 0 if current == 0 else (1 if halved and signal_long else 2)
        if state != prev_state:
            nav_after_cost = nav * (1 - SWITCH_COST * abs(target - current))
            px = float(df["open"].iloc[i + 1])
            shares = nav_after_cost * target / px
            cash = nav_after_cost * (1 - target)
            switches += 1
        invested_days += int(shares > 0)
        equity.append(cash + shares * float(df["close"].iloc[i + 1]))
    eq = pd.Series(equity)
    bh = df["close"].iloc[1:].reset_index(drop=True) / float(df["close"].iloc[0])
    years = len(eq) / 243
    return {
        "total_pct": round((float(eq.iloc[-1]) - 1) * 100, 2),
        "ann_pct": round((float(eq.iloc[-1]) ** (1 / years) - 1) * 100, 2),
        "mdd_pct": round(float((eq / eq.cummax() - 1).min() * 100), 2),
        "bh_total_pct": round((float(bh.iloc[-1]) - 1) * 100, 2),
        "bh_mdd_pct": round(float((bh / bh.cummax() - 1).min() * 100), 2),
        "switches": switches,
        "invested_pct": round(invested_days / len(eq) * 100, 1),
        "_equity": eq,
        "_dates": list(df["date"].iloc[1:]),
        "_bh": bh,
    }


def segment_mdd(result: dict[str, Any]) -> dict[str, Any]:
    eq, bh, dates = result["_equity"], result["_bh"], pd.Series(result["_dates"])
    out = {}
    for lo, hi in SEGMENTS:
        mask = (dates.str[:4] >= lo) & (dates.str[:4] <= hi)
        if int(mask.sum()) < 60:
            continue
        e, b = eq[mask.values].reset_index(drop=True), bh[mask.values].reset_index(drop=True)
        out[f"{lo}-{hi}"] = {
            "timing_mdd": round(float((e / e.cummax() - 1).min() * 100), 2),
            "bh_mdd": round(float((b / b.cummax() - 1).min() * 100), 2),
            "timing_ret": round((float(e.iloc[-1] / e.iloc[0]) - 1) * 100, 2),
            "bh_ret": round((float(b.iloc[-1] / b.iloc[0]) - 1) * 100, 2),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="T4 ETF MA 择时回测")
    parser.add_argument("--symbol", default="512100.SH")
    parser.add_argument("--out", default="tmp/t4_etf_result.json")
    args = parser.parse_args()
    load_dotenv(".env")
    df = load_etf(args.symbol)
    result: dict[str, Any] = {"symbol": args.symbol, "days": len(df)}
    for window in (40, 60, 90):
        run = run_timing(df, window)
        result[f"ma{window}"] = {k: v for k, v in run.items() if not k.startswith("_")}
        result[f"ma{window}_segments"] = segment_mdd(run)
    breaker_run = run_timing(df, 60, breaker=True)
    result["ma60_breaker"] = {k: v for k, v in breaker_run.items() if not k.startswith("_")}
    result["ma60_breaker_segments"] = segment_mdd(breaker_run)
    for window in (40, 60, 90):
        dual_run = run_timing(df, window, dual=True)
        result[f"ma{window}_dual"] = {k: v for k, v in dual_run.items() if not k.startswith("_")}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
