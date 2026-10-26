"""牛市段龙头捕捉审计：解析 backtest_grid 的 bull_2025 产物（只读分析）。

输入：analysis/backtest_grid_actions/bull_2025/ 下各 grid cell 目录。
判据（事前定死）：现金组合跑赢指数？大赢家来自趋势类信号还是抄底类？
trades_*.csv 的 ret_pct 已含回测摩擦（买/卖各 0.5%，严于实盘 0.202%）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "analysis/backtest_grid_actions/bull_2025")
CONTROL = Path(sys.argv[2]) if len(sys.argv) > 2 else None
OUT = Path("tmp/audit_bull_2025")
LEADER_TRIGGERS = {"trend_breakout", "main_force_entry", "early_breakout", "sos", "sector_strength", "launchpad"}


def stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0}
    r = df["ret_pct"].astype(float)
    return {
        "n": int(len(r)),
        "win_rate": round(float((r > 0).mean() * 100), 2),
        "mean": round(float(r.mean()), 4),
        "median": round(float(r.median()), 4),
        "sum": round(float(r.sum()), 2),
    }


def by_col(df: pd.DataFrame, col: str) -> dict:
    return {str(k): stats(g) for k, g in df.groupby(col) if len(g) >= 3}


def load_trades(path: Path) -> pd.DataFrame | None:
    files = [path] if path.is_file() else sorted(path.rglob("trades_*.csv"))
    if not files:
        return None
    df = pd.read_csv(files[0])
    df["ret_pct"] = pd.to_numeric(df["ret_pct"], errors="coerce")
    return df.dropna(subset=["ret_pct"])


def trade_key(df: pd.DataFrame) -> pd.Series:
    code = df["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    return df["signal_date"].astype(str) + "|" + code


def compare_arms(live: pd.DataFrame, control: pd.DataFrame) -> dict:
    """按 AGENTS Rule 9 用 signal_date+code 配对，拆 common / 两边独有集。"""
    live_k, ctrl_k = trade_key(live), trade_key(control)
    common, live_only, ctrl_only = set(live_k) & set(ctrl_k), set(live_k) - set(ctrl_k), set(ctrl_k) - set(live_k)
    return {
        "common_n": len(common),
        "live_only": stats(live[live_k.isin(live_only)]),
        "control_only": stats(control[ctrl_k.isin(ctrl_only)]),
        "common_live": stats(live[live_k.isin(common)]) if common else {"n": 0},
        "common_control": stats(control[ctrl_k.isin(common)]) if common else {"n": 0},
        "control_by_trigger": by_col(control, "trigger") if "trigger" in control.columns else {},
        "control_by_regime": by_col(control, "regime") if "regime" in control.columns else {},
        "control_winners_ge_15": int((control["ret_pct"] >= 15).sum()),
        "control_top10": control.nlargest(10, "ret_pct")[
            [
                c
                for c in ("signal_date", "code", "name", "trigger", "ret_pct", "prior_mom20_pct")
                if c in control.columns
            ]
        ].to_dict("records"),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cells = sorted(ROOT.glob("backtest-grid-bull_2025-*"))
    print(f"[bull] cells={len(cells)} root={ROOT}")
    summaries = {}
    for c in cells:
        for md in c.glob("summary_*.md"):
            summaries[c.name] = md.read_text(encoding="utf-8")[:6000]
    # 基准 cell：hold10 无止盈止损（目录名含 h10 与 sl0 tp0 的 stamp 在文件名里）
    base_trades = None
    for c in cells:
        if "h10" in c.name and "sl0" in c.name.replace("-", "") and "tp0" in c.name.replace("-", ""):
            for f in c.glob("trades_*.csv"):
                base_trades = pd.read_csv(f)
                print(f"[bull] base cell={c.name} trades={len(base_trades)} cols={list(base_trades.columns)}")
    if base_trades is None:
        # 退化：取第一个 trades 文件并打印结构供人工判断
        for c in cells:
            fs = list(c.glob("trades_*.csv"))
            if fs:
                base_trades = pd.read_csv(fs[0])
                print(f"[bull] fallback cell={c.name} trades={len(base_trades)} cols={list(base_trades.columns)}")
                break
    result: dict = {"cells": [c.name for c in cells], "summaries_md": summaries}
    if base_trades is not None and not base_trades.empty:
        df = base_trades.copy()
        df["ret_pct"] = pd.to_numeric(df["ret_pct"], errors="coerce")
        df = df.dropna(subset=["ret_pct"])
        trig_col = "trigger" if "trigger" in df.columns else "signal_type"
        result["base_overall"] = stats(df)
        result["by_trigger"] = by_col(df, trig_col)
        if "track" in df.columns:
            result["by_track"] = by_col(df, "track")
        if "regime" in df.columns:
            result["by_regime"] = by_col(df, "regime")
        # 龙头捕捉：大赢家（>=+15% / >=+20%）的信号构成 vs 全体构成
        for thr in (15.0, 20.0):
            win = df[df["ret_pct"] >= thr]
            result[f"winners_ge_{thr}"] = {
                "n": int(len(win)),
                "share_of_all_pct": round(len(win) / len(df) * 100, 2) if len(df) else None,
                "by_trigger": {str(k): int(v) for k, v in win[trig_col].value_counts().items()},
                "leader_trigger_share_pct": round(float(win[trig_col].isin(LEADER_TRIGGERS).mean() * 100), 2)
                if len(win)
                else None,
                "all_leader_trigger_share_pct": round(float(df[trig_col].isin(LEADER_TRIGGERS).mean() * 100), 2),
                "mean_prior_mom20": round(float(win["prior_mom20_pct"].mean()), 3)
                if "prior_mom20_pct" in df.columns and len(win)
                else None,
            }
        losers = df[df["ret_pct"] <= -10]
        result["losers_le_-10"] = {
            "n": int(len(losers)),
            "by_trigger": {str(k): int(v) for k, v in losers[trig_col].value_counts().items()} if len(losers) else {},
        }
        if "prior_mom20_pct" in df.columns:
            q = df.dropna(subset=["prior_mom20_pct"]).copy()
            if len(q) >= 20:
                q["mom_q"] = pd.qcut(
                    q["prior_mom20_pct"], 5, labels=["Q1低动量", "Q2", "Q3", "Q4", "Q5高动量"], duplicates="drop"
                )
                result["by_prior_mom20_quintile"] = by_col(q, "mom_q")
        if CONTROL is not None:
            ctrl = load_trades(CONTROL)
            if ctrl is not None:
                result["control_overall"] = stats(ctrl)
                result["live_vs_control_marginal"] = compare_arms(df, ctrl)
        if "mfe_pct" in df.columns:
            result["top10_trades"] = df.nlargest(10, "ret_pct")[
                [
                    c
                    for c in ("signal_date", "code", "name", trig_col, "ret_pct", "mfe_pct", "prior_mom20_pct")
                    if c in df.columns
                ]
            ].to_dict("records")
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps({k: v for k, v in result.items() if k != "summaries_md"}, ensure_ascii=False, indent=2, default=str)[
            :6000
        ]
    )
    print(f"[bull] written {OUT / 'result.json'}")


if __name__ == "__main__":
    main()
