#!/usr/bin/env python3
"""Exchange-verified National Team 20-ETF daily share creation/redemption & cost-band ledger."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[3]
for _p in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intraday_radar import load_env_file  # noqa: E402
from telegraph_catalysts import BLUECHIP_FLOOR_ETFS, NATIONAL_TEAM_20_ETFS  # noqa: E402

from integrations.tickflow_client import TickFlowClient  # noqa: E402
from utils.trading_clock import CN_TZ  # noqa: E402

SSE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("3800 ~ 3850点", 3800.0, 4000.0),
    ("3700 ~ 3800点", 3700.0, 3800.0),
    ("3650 ~ 3700点", 3650.0, 3700.0),
    ("3600 ~ 3650点", 3600.0, 3650.0),
    ("3550 ~ 3600点", 3550.0, 3600.0),
    ("3500 ~ 3550点", 3500.0, 3550.0),
    ("3455 ~ 3500点", 0.0, 3500.0),
)


def _fetch_share_flows(
    pro: Any,
    tf: TickFlowClient,
    start_date: str,
    end_date: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Compute per-day and per-ETF creation/redemption flows in 亿元."""
    fetch_start = f"{int(start_date[:4]) - 1}{start_date[4:]}" if len(start_date) == 8 else start_date
    daily: dict[str, dict[str, Any]] = {}
    per_etf: dict[str, dict[str, Any]] = {}
    for sym, label in NATIONAL_TEAM_20_ETFS.items():
        fs = pro.fund_share(ts_code=sym, start_date=fetch_start, end_date=end_date)
        if fs is None or len(fs) < 2:
            continue
        fs = fs.sort_values("trade_date").reset_index(drop=True)
        fs["d_sh_wan"] = fs["fd_share"] - fs["fd_share"].shift(1)
        kl = tf.get_klines(sym, period="1d", count=90)
        p_map = {str(r["date"]).replace("-", ""): float(r["close"]) for _, r in kl.iterrows()}
        sub = fs[(fs["trade_date"] >= start_date) & (fs["trade_date"] <= end_date)].dropna(subset=["d_sh_wan"])
        is_blue = sym in BLUECHIP_FLOOR_ETFS
        net_s, in_s, out_s = 0.0, 0.0, 0.0
        for _, row in sub.iterrows():
            ymd = str(row["trade_date"])
            iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
            flow = float(row["d_sh_wan"]) * p_map.get(ymd, 0.0) / 10000.0
            net_s += flow
            in_s += flow if flow > 0 else 0.0
            out_s += flow if flow < 0 else 0.0
            rec = daily.setdefault(iso, {"tot": 0.0, "blue": 0.0, "grow": 0.0, "top": []})
            rec["tot"] += flow
            rec["blue" if is_blue else "grow"] += flow
            if abs(flow) >= 3.0:
                rec["top"].append((flow, sym[:6]))
        if len(sub) > 0:
            per_etf[sym] = {"label": label, "is_blue": is_blue, "net": net_s, "in": in_s, "out": out_s}
    return daily, per_etf


def _print_band_summary(rows: list[dict[str, Any]], tot_net: float) -> None:
    print("\n=== 上证点位筹码峰分布 ===")
    for label, lo, hi in SSE_BANDS:
        sub = [r for r in rows if lo <= r["sh"] < hi]
        net_s = sum(r["tot"] for r in sub)
        buys = sum(1 for r in sub if r["tot"] > 0)
        sells = sum(1 for r in sub if r["tot"] < 0)
        pct = (net_s / tot_net * 100.0) if tot_net else 0.0
        print(f"{label:16s} | {len(sub):2d}天({buys}买/{sells}卖) | 净={net_s:+7.1f}亿 | 占比={pct:5.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="National Team 20-ETF fund_share inflow/outflow ledger")
    parser.add_argument("--start-date", default="20260928", help="YYYYMMDD start date")
    parser.add_argument("--end-date", default=datetime.now(CN_TZ).strftime("%Y%m%d"), help="YYYYMMDD end date")
    args = parser.parse_args()

    load_env_file()
    ts_token = os.environ.get("TUSHARE_TOKEN", "").strip()
    tf_key = os.environ.get("TICKFLOW_API_KEY", "").strip()
    if not ts_token or not tf_key:
        print("[ERROR] Missing TUSHARE_TOKEN or TICKFLOW_API_KEY", file=sys.stderr)
        sys.exit(1)

    import tushare as ts

    pro = ts.pro_api(ts_token)
    tf = TickFlowClient(tf_key)
    sh_df = tf.get_klines("000001.SH", period="1d", count=90)
    sh_map = {str(r["date"]): float(r["close"]) for _, r in sh_df.iterrows()}
    daily, per_etf = _fetch_share_flows(pro, tf, args.start_date, args.end_date)

    tot_net = sum(v["net"] for v in per_etf.values())
    blue_net = sum(v["net"] for v in per_etf.values() if v["is_blue"])
    grow_net = sum(v["net"] for v in per_etf.values() if not v["is_blue"])
    rows = [{"date": d, "sh": sh_map.get(d, 0.0), **daily[d]} for d in sorted(daily)]
    pos = [r for r in rows if r["tot"] > 0 and r["sh"] > 0]
    w_cost = sum(r["sh"] * r["tot"] for r in pos) / max(sum(r["tot"] for r in pos), 1e-6)
    print(f"合计净流入={tot_net:+.1f}亿 (蓝筹{blue_net:+.1f} / 成长{grow_net:+.1f}) | 加权上证成本={w_cost:.1f}点")
    _print_band_summary(rows, tot_net)
    print("\n=== 逐日进出台账 ===")
    cum = 0.0
    for r in rows:
        cum += r["tot"]
        tops = ", ".join(f"{c}({v:+.1f})" for v, c in sorted(r["top"], key=lambda x: -abs(x[0]))[:3])
        print(
            f"{r['date']} | 上证={r['sh']:7.1f} | 净={r['tot']:+7.1f}亿 (蓝{r['blue']:+6.1f}/成{r['grow']:+6.1f}) | 累计={cum:+8.1f}亿 | {tops}"
        )


if __name__ == "__main__":
    main()
