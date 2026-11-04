#!/usr/bin/env python3
"""Full-market 5-minute intraday differential radar & 4-tier Feishu urgent notifier."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from integrations.tickflow_client import TickFlowClient
from utils.feishu_report_card import build_report_card_elements, report_card_template

DEFAULT_USER_OPEN_ID = "ou_265396a9e26fff2541aed28ef025c2c2"
STATE_PATH = Path("/tmp/wyckoff_intraday_radar_state.json")


def load_wyckoff_l2_map(trace_path: str | None) -> dict[str, dict[str, Any]]:
    """Load overnight review_trace.json.gz to map 6-digit code -> L2 channel & RPS."""
    if not trace_path or not Path(trace_path).exists():
        candidates = sorted(Path("/Users/youngcan/.gemini/antigravity/brain").rglob("review_trace_*.json.gz"))
        if not candidates:
            return {}
        trace_path = str(candidates[-1])
    with gzip.open(trace_path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("symbols") or {}


def compute_quote_metrics(q: dict[str, Any]) -> dict[str, Any] | None:
    """Extract price, pct, vwap, bias, and amount (in 100M RMB) from a TickFlow quote."""
    last = float(q.get("last_price") or 0)
    prev = float(q.get("prev_close") or 0)
    vol = float(q.get("volume") or 0)
    amt = float(q.get("amount") or 0)
    if last <= 0 or prev <= 0:
        return None
    pct = (last - prev) / prev * 100.0
    vwap = amt / vol if vol > 0 else last
    if vwap > last * 10:
        vwap = vwap / 100.0
    bias = (last - vwap) / vwap * 100.0 if vwap > 0 else 0.0
    name = (q.get("ext") or {}).get("name", "") or q.get("name", "")
    return {
        "name": name,
        "last": last,
        "prev": prev,
        "high": float(q.get("high") or last),
        "low": float(q.get("low") or last),
        "pct": pct,
        "vwap": vwap,
        "bias": bias,
        "amt_yi": amt / 1e8,
    }


def send_feishu_tiered_alert(
    title: str,
    content: str,
    *,
    level: str = "L1",
    user_open_id: str = DEFAULT_USER_OPEN_ID,
    dry_run: bool = False,
) -> str | None:
    """Send Feishu interactive card and optionally trigger L2/L3/L4 buzz (app/sms/phone)."""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"[{level}] {title}"},
            "template": report_card_template(title, content),
        },
        "elements": build_report_card_elements(content),
    }
    if dry_run:
        print(f"[DRY-RUN {level}] {title}\n{content}")
        return "dry_run_msg_id"
    cmd = [
        "lark-cli",
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--user-id",
        user_open_id,
        "--msg-type",
        "interactive",
        "--content",
        json.dumps(card, ensure_ascii=False),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(res.stdout or "{}")
        msg_id = (payload.get("data") or {}).get("message_id")
    except Exception:
        msg_id = None
    if not msg_id:
        return None
    urgent_map = {"L2": "urgent_app", "L3": "urgent_sms", "L4": "urgent_phone"}
    subcmd = urgent_map.get(level)
    if subcmd:
        subprocess.run(
            [
                "lark-cli",
                "im",
                "messages",
                subcmd,
                "--as",
                "bot",
                "--message-id",
                msg_id,
                "--user-id-type",
                "open_id",
                "--data",
                json.dumps({"user_list": [user_open_id]}),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    return msg_id


def _check_holdings(
    curr_quotes: dict[str, dict[str, Any]],
    prev_snaps: dict[str, Any],
    holdings: dict[str, dict[str, float]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Evaluate holding positions against VWAP and stop-loss thresholds."""
    lines, l4, l3, l2 = [], [], [], []
    for sym, h_cfg in holdings.items():
        m = compute_quote_metrics(curr_quotes.get(sym) or {})
        if not m:
            continue
        p = prev_snaps.get(sym) or {}
        dpct = m["pct"] - float(p.get("pct", m["pct"]))
        stop = float(h_cfg.get("stop", 0))
        lines.append(
            f"- **{sym} {m['name']}**：现价 **{m['last']:.2f} ({m['pct']:+.2f}%)** | "
            f"均价 {m['vwap']:.2f} (乖离 {m['bias']:+.2f}%) | 5m涨速 {dpct:+.2f}% | 额 {m['amt_yi']:.2f}亿"
        )
        if stop > 0 and m["last"] <= stop:
            l4.append(f"🔴 **持仓跌破止损**：{sym} {m['name']} 现价 {m['last']:.2f} <= 止损 {stop:.2f}！")
        elif dpct <= -2.0 and m["last"] < m["vwap"]:
            l3.append(
                f"🟠 **持仓5分钟急跌破均线**：{sym} {m['name']} 5m跌 {dpct:.2f}%，现价 {m['last']:.2f} < 均 {m['vwap']:.2f}"
            )
        elif m["bias"] >= 3.0:
            l2.append(f"🟡 **持仓高抛做T波峰**：{sym} {m['name']} 偏离均线 +{m['bias']:.2f}% ({m['last']:.2f})")
    return lines, l4, l3, l2


def analyze_snapshot_diff(
    curr_quotes: dict[str, dict[str, Any]],
    prev_state: dict[str, Any],
    l2_map: dict[str, dict[str, Any]],
    holdings: dict[str, dict[str, float]],
) -> tuple[str, str, str]:
    """Compare current 7,807 quotes against previous 5-min snapshot and classify L1~L4."""
    prev_snaps = prev_state.get("quotes") or {}
    sh_m = compute_quote_metrics(curr_quotes.get("000001.SH") or {})
    sz_m = compute_quote_metrics(curr_quotes.get("399001.SZ") or {})
    total_amt_yi = (sh_m["amt_yi"] if sh_m else 0.0) + (sz_m["amt_yi"] if sz_m else 0.0)
    sh_pct, sz_pct = (sh_m["pct"] if sh_m else 0.0), (sz_m["pct"] if sz_m else 0.0)
    holding_lines, l4_emergencies, l3_ignitions, l2_surges = _check_holdings(curr_quotes, prev_snaps, holdings)
    etf_surges: list[str] = []

    for sym, q in curr_quotes.items():
        m = compute_quote_metrics(q)
        if not m:
            continue
        p = prev_snaps.get(sym) or {}
        dpct = m["pct"] - float(p.get("pct", m["pct"]))
        damt = m["amt_yi"] - float(p.get("amt_yi", m["amt_yi"]))
        if sym.startswith(("51", "56", "58", "159")) and m["amt_yi"] >= 0.8 and dpct >= 1.0 and m["bias"] >= 0:
            etf_surges.append(
                f"- **{sym} {m['name']}**：现价 {m['last']:.3f} ({m['pct']:+.2f}%) | 5m急涨 **{dpct:+.2f}%** | 额 {m['amt_yi']:.2f}亿"
            )
        elif sym[:2] in ("00", "30", "60", "68") and not sym.startswith(("51", "56", "58", "159")):
            d = l2_map.get(sym[:6]) or {}
            if (
                d.get("l2_eligible")
                and dpct >= 1.8
                and damt >= 0.25
                and 2.5 <= m["pct"] <= 5.5
                and 0 <= m["bias"] <= 2.0
            ):
                ch, rps = d.get("l2_channel", "L2"), float(d.get("rps_fast") or 0)
                l3_ignitions.append(
                    f"- **{sym} {m['name']}** [{d.get('sector', '')}]：现价 **{m['last']:.2f} ({m['pct']:+.2f}%)** | "
                    f"5m涨速 **{dpct:+.2f}%** (5m增额 {damt:.2f}亿) | 均价 {m['vwap']:.2f} ✅ | {ch} RPS={rps:.0f}"
                )
            elif dpct >= 2.8 and damt >= 0.40 and 2.0 <= m["pct"] <= 6.5 and m["bias"] >= 0:
                l2_surges.append(
                    f"- **{sym} {m['name']}**：现价 {m['last']:.2f} ({m['pct']:+.2f}%) | 5m急涨 **{dpct:+.2f}%** | 均价 {m['vwap']:.2f}"
                )

    level = "L4" if l4_emergencies else ("L3" if l3_ignitions else ("L2" if (l2_surges or etf_surges) else "L1"))
    now_str = datetime.now().strftime("%H:%M:%S")
    title = f"{now_str} 全市场7807标的雷达：两市 {total_amt_yi:.0f}亿 | 沪 {sh_pct:+.2f}% 深 {sz_pct:+.2f}%"
    sections = [
        f"全市场 7,807 标的实时扫描完成，两市合计成交 {total_amt_yi:.0f} 亿元（上证 {sh_pct:+.2f}%，深成指 {sz_pct:+.2f}%）。",
        "**1. 持仓与重点标的实时监控**\n" + ("\n".join(holding_lines) if holding_lines else "- 暂无持仓配置"),
    ]
    if l4_emergencies:
        sections.append("**2. 🔴 L4 电话级紧急信号**\n" + "\n".join(l4_emergencies[:5]))
    if l3_ignitions:
        sections.append("**3. 🟠 L3 短信级：威科夫 L2 地量/吸筹 5 分钟倍量点火**\n" + "\n".join(l3_ignitions[:8]))
    if etf_surges:
        sections.append("**4. 🟡 全市场 ETF 5 分钟急涨风口**\n" + "\n".join(etf_surges[:6]))
    if l2_surges:
        sections.append("**5. 🟡 全市场个股 5 分钟火箭发射**\n" + "\n".join(l2_surges[:8]))
    sections.append("⚠️ 风险提醒: 严守分时均价线（VWAP）与今日真实量比双确认，绝不在均线下方接飞刀。")
    return level, title, "\n\n".join(sections)


def run_once(args: argparse.Namespace) -> None:
    """Execute one full-market scan cycle and persist state for 5-min diff."""
    load_dotenv(".env")
    client = TickFlowClient(api_key=os.environ.get("TICKFLOW_API_KEY", ""))
    l2_map = load_wyckoff_l2_map(args.trace)
    prev_state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    curr_quotes = client.get_quotes(universes=["CN_Equity_A", "CN_ETF", "CN_Index"])
    holdings = {"600398.SH": {"cost": 5.828, "stop": 5.86}}
    level, title, content = analyze_snapshot_diff(curr_quotes, prev_state, l2_map, holdings)

    compact_snaps = {}
    for sym, q in curr_quotes.items():
        m = compute_quote_metrics(q)
        if m:
            compact_snaps[sym] = {"pct": round(m["pct"], 3), "amt_yi": round(m["amt_yi"], 3)}
    STATE_PATH.write_text(json.dumps({"ts": time.time(), "quotes": compact_snaps}), encoding="utf-8")

    should_send = args.force_send or level in ("L2", "L3", "L4")
    if should_send:
        msg_id = send_feishu_tiered_alert(title, content, level=level, dry_run=args.dry_run)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sent {level} alert: msg_id={msg_id}")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan ok (7,807 quotes), level={level}, no urgent trigger.")


def main() -> None:
    parser = argparse.ArgumentParser(description="5-minute full-market intraday radar")
    parser.add_argument("--once", action="store_true", help="Run a single scan")
    parser.add_argument("--daemon", action="store_true", help="Run every N seconds during market hours")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval seconds (default 300)")
    parser.add_argument("--trace", type=str, default=None, help="Path to review_trace_*.json.gz")
    parser.add_argument("--force-send", action="store_true", help="Always send Feishu card even if L1")
    parser.add_argument("--dry-run", action="store_true", help="Print card without sending to Feishu")
    args = parser.parse_args()
    if args.daemon:
        while True:
            run_once(args)
            time.sleep(max(args.interval, 30))
    else:
        run_once(args)


if __name__ == "__main__":
    main()
