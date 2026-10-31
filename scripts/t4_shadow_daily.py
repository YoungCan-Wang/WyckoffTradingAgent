"""T4 影子盘每日记账（无状态复算，规格见 Issue #592）。

每天从影子起始日全量复算 159915 的 MA60+熔断模拟，输出当日信号与待执行动作。
起始日固定，任意一天重跑结果一致；成交按信号次日开盘、每次切换 0.10%。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401
import pandas as pd
from dotenv import load_dotenv

from scripts.research_t4_etf_timing import SWITCH_COST, load_etf

SHADOW_START = "2026-10-28"
INITIAL_CASH = 100_000.0
MA_WINDOW = 60


def simulate(df: pd.DataFrame, start: str = SHADOW_START) -> dict[str, Any]:
    df = df.copy()
    df["ma"] = df["close"].rolling(MA_WINDOW).mean()
    df = df.dropna().reset_index(drop=True)
    dates = [str(d) for d in df["date"]]
    begin = next((i for i, d in enumerate(dates) if d >= start), len(dates) - 1)
    cash, shares, peak, halved = INITIAL_CASH, 0.0, INITIAL_CASH, False
    ledger: list[dict[str, Any]] = []
    bh_shares = INITIAL_CASH / float(df["open"].iloc[begin]) if begin < len(df) else 0.0
    for i in range(begin, len(df) - 1):
        close, ma = float(df["close"].iloc[i]), float(df["ma"].iloc[i])
        nav = cash + shares * close
        peak = max(peak, nav)
        if i % 5 == 0:
            dd = nav / peak - 1
            halved = dd <= -0.12 if not halved else dd < -0.06
        signal_long = close > ma
        target = (0.5 if halved else 1.0) if signal_long else 0.0
        state = 0 if target == 0 else (1 if target == 0.5 else 2)
        prev = 0 if shares == 0 else (1 if halved and signal_long else 2)
        action = "hold"
        if state != prev:
            nav *= 1 - SWITCH_COST * abs(target - (shares * close / nav if nav else 0))
            px = float(df["open"].iloc[i + 1])
            shares, cash = nav * target / px, nav * (1 - target)
            action = {0: "sell_all_next_open", 1: "half_next_open", 2: "buy_all_next_open"}[state]
        ledger.append(
            {
                "date": dates[i],
                "close": round(close, 3),
                "ma60": round(ma, 3),
                "signal": "long" if signal_long else "cash",
                "breaker_halved": halved,
                "action_for_next_open": action,
                "nav": round(cash + shares * close, 2),
            }
        )
    last_close = float(df["close"].iloc[-1])
    nav = cash + shares * last_close
    bh_nav = nav if dates[-1] < start else bh_shares * last_close
    return {
        "shadow_start": start,
        "as_of": dates[-1],
        "nav": round(nav, 2),
        "ret_pct": round((nav / INITIAL_CASH - 1) * 100, 3),
        "bh_ret_pct": round((bh_nav / INITIAL_CASH - 1) * 100, 3),
        "position": "full" if shares and not halved else ("half" if shares else "cash"),
        "ledger_tail": ledger[-5:],
        "trades": sum(1 for row in ledger if row["action_for_next_open"] != "hold"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="T4 影子盘每日记账")
    parser.add_argument("--symbol", default="159915.SZ")
    parser.add_argument("--out", default="tmp/t4_shadow.json")
    parser.add_argument("--markdown", default="")
    args = parser.parse_args()
    load_dotenv(".env")
    result = simulate(load_etf(args.symbol, count=400))
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tail = result["ledger_tail"][-1] if result["ledger_tail"] else {}
    lines = [
        "## T4 影子盘日报",
        "",
        f"- 日期：{result['as_of']}（影子起始 {result['shadow_start']}）",
        f"- 净值：{result['nav']:.2f}（{result['ret_pct']:+.3f}%），买入持有 {result['bh_ret_pct']:+.3f}%",
        f"- 当前仓位：{result['position']}，累计调仓 {result['trades']} 次",
        f"- 最新信号：{tail.get('signal')}，次日开盘动作：{tail.get('action_for_next_open')}",
        f"- 收盘 {tail.get('close')} / MA60 {tail.get('ma60')}，熔断半仓：{tail.get('breaker_halved')}",
    ]
    text = "\n".join(lines)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
