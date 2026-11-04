#!/usr/bin/env python3
"""Full-market 5-minute intraday radar (09:30-15:00 BJT, 7,807 instruments) with 4-tier Feishu alerts."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.trading_clock import CN_TZ, cn_today, is_a_share_trading_day  # noqa: E402

STATE_PATH = Path("/tmp/wyckoff_intraday_radar_state.json")
DEFAULT_CONFIG_PATH = Path.home() / ".wyckoff" / "intraday_radar_config.json"
COOLDOWN_SECONDS = 1800


def load_env_file() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_radar_config(config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH
    cfg: dict[str, Any] = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    open_id = os.environ.get("FEISHU_USER_OPEN_ID", "").strip() or str(cfg.get("feishu_user_open_id", "")).strip()
    holdings = cfg.get("holdings") if isinstance(cfg.get("holdings"), dict) else {}
    env_trace_dir = os.environ.get("WYCKOFF_TRACE_DIR", "").strip()
    trace_dirs = [env_trace_dir] if env_trace_dir else list(cfg.get("trace_dirs", []))
    for fallback in [str(Path.home() / ".wyckoff" / "traces"), "/tmp", str(REPO_ROOT / "data")]:
        if fallback not in trace_dirs:
            trace_dirs.append(fallback)
    return {"feishu_user_open_id": open_id, "holdings": holdings, "trace_dirs": trace_dirs}


def is_cn_trading_session(now: datetime | None = None) -> bool:
    dt = now.astimezone(CN_TZ) if now else datetime.now(CN_TZ)
    if not is_a_share_trading_day(dt.date()):
        return False
    hm = (dt.hour, dt.minute)
    return (9, 25) <= hm <= (11, 35) or (12, 55) <= hm <= (15, 5)


def is_a_share(sym: str) -> bool:
    if sym.endswith(".SH"):
        return sym[:2] in ("60", "68")
    if sym.endswith(".SZ"):
        return sym[:2] in ("00", "30")
    return sym.endswith(".BJ") and len(sym) == 9 and sym[:6].isdigit()


def is_etf(sym: str) -> bool:
    if sym.endswith(".SH"):
        return sym[:2] in ("51", "56", "58")
    return sym.endswith(".SZ") and sym[:2] == "15"


def load_wyckoff_l2_map(
    trace_path: str | None = None, trace_dirs: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    candidates: list[Path] = []
    if trace_path and Path(trace_path).exists():
        candidates.append(Path(trace_path))
    else:
        for raw_dir in trace_dirs or [str(Path.home() / ".wyckoff" / "traces"), "/tmp", str(REPO_ROOT / "data")]:
            base = Path(raw_dir).expanduser()
            if base.exists():
                candidates.extend(base.rglob("review_trace_*.json.gz"))
    if not candidates:
        return {}
    latest = sorted(candidates, key=lambda p: p.name)[-1]
    with gzip.open(latest, "rt", encoding="utf-8") as f:
        data = json.load(f)
    out: dict[str, dict[str, Any]] = {}
    raw_syms = data.get("symbols", {})
    entries = (
        raw_syms.items()
        if isinstance(raw_syms, dict)
        else ((s.get("symbol", ""), s) for s in raw_syms if isinstance(s, dict))
    )
    for raw_key, s in entries:
        if not isinstance(s, dict):
            continue
        code6 = str(raw_key or s.get("symbol", ""))[:6]
        raw_ch = s.get("l2_channels") or s.get("l2_channel") or []
        chans = [raw_ch] if isinstance(raw_ch, str) else list(raw_ch)
        tier = str(s.get("actionable_tier") or s.get("tier") or s.get("shadow_lane") or "")
        out[code6] = {
            "name": s.get("name", ""),
            "sector": s.get("sector", ""),
            "rps": float(s.get("rps_fast") or 0.0),
            "l2_eligible": bool(s.get("l2_eligible")),
            "l3_eligible": bool(s.get("l3_eligible")),
            "channels": chans,
            "prev_amt_yi": float(s.get("amount_yi") or s.get("prev_amount_yi") or 0.0),
            "is_springboard": ("A+B+C" in tier) or ("启动平台" in tier) or bool(s.get("is_springboard")),
        }
    return out


def compute_quote_metrics(q: dict[str, Any]) -> dict[str, Any] | None:
    last_p = float(q.get("last_price") or 0.0)
    prev_c = float(q.get("prev_close") or 0.0)
    vol = float(q.get("volume") or 0.0)
    amt = float(q.get("amount") or 0.0)
    if last_p <= 0 or prev_c <= 0:
        return None
    pct = (last_p / prev_c - 1.0) * 100.0
    ext = q.get("ext") or {}
    name = ext.get("name") or q.get("name") or ""
    raw_vwap = amt / vol if vol > 0 else last_p
    if 0.5 < (raw_vwap / last_p) < 1.5:
        vwap = raw_vwap
    elif 0.5 < (raw_vwap / 100.0 / last_p) < 1.5:
        vwap = raw_vwap / 100.0
    else:
        vwap = (float(q.get("high") or last_p) + float(q.get("low") or last_p) + last_p) / 3.0
    vwap_dev = (last_p / vwap - 1.0) * 100.0 if vwap > 0 else 0.0
    return {
        "symbol": q.get("symbol", ""),
        "name": name,
        "last": last_p,
        "prev_close": prev_c,
        "pct": pct,
        "amt_yi": amt / 1e8,
        "vwap": vwap,
        "vwap_dev": vwap_dev,
    }


def _check_cooldown(cooldowns: dict[str, float], key: str, now_ts: float) -> bool:
    if key in cooldowns and now_ts - float(cooldowns[key]) < COOLDOWN_SECONDS:
        return False
    cooldowns[key] = now_ts
    return True


def _evaluate_holdings(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]],
    has_baseline: bool,
    holdings: dict[str, dict[str, float]],
    cooldowns: dict[str, float],
    now_ts: float,
) -> tuple[list[str], list[str], list[str]]:
    lines: list[str] = []
    l3_alerts: list[str] = []
    l4_alerts: list[str] = []
    for sym, cfg in holdings.items():
        m = compute_quote_metrics(curr_quotes.get(sym, {}))
        if not m:
            continue
        prev_s = prev_q.get(sym) if has_baseline else None
        dpct = m["pct"] - prev_s["pct"] if prev_s else 0.0
        stop = float(cfg.get("stop", 0.0))
        tag = ""
        if stop > 0 and m["last"] < stop and m["amt_yi"] > 0.5 and _check_cooldown(cooldowns, f"L4:{sym}", now_ts):
            l4_alerts.append(f"🔴 **【L4 破位止损】{sym} {m['name']}** 跌破止损 {stop}（现价 {m['last']:.2f}）")
            tag = " 🔴破止损!"
        elif dpct <= -2.0 and m["last"] < m["vwap"] and _check_cooldown(cooldowns, f"L3:{sym}", now_ts):
            l3_alerts.append(f"🟠 **【持仓急跌】{sym} {m['name']}** 5m 急跌 {dpct:.2f}% 且跌破均线 {m['vwap']:.2f}")
            tag = " 🟠急跌!"
        elif m["vwap_dev"] > 3.0 and _check_cooldown(cooldowns, f"L2:{sym}", now_ts):
            tag = " 🟡偏离均线>3%可做T高抛"
        lines.append(
            f"- **{sym} {m['name']}**：现价 **{m['last']:.2f} ({m['pct']:+.2f}%)** | "
            f"均价 {m['vwap']:.2f} (乖离 {m['vwap_dev']:+.2f}%) | 5m涨速 {dpct:+.2f}% | 额 {m['amt_yi']:.2f}亿{tag}"
        )
    return lines, l3_alerts, l4_alerts


def _scan_equities_and_etfs(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]],
    has_baseline: bool,
    l2_map: dict[str, dict[str, Any]],
    red_ratio: float,
    cooldowns: dict[str, float],
    now_ts: float,
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    sector_surges: dict[str, list[str]] = defaultdict(list)
    for sym, q in curr_quotes.items():
        if "ST" in str((q.get("ext") or {}).get("name", "")):
            continue
        m = compute_quote_metrics(q)
        if not m:
            continue
        prev_s = prev_q.get(sym) if has_baseline else None
        dpct = m["pct"] - prev_s["pct"] if prev_s else 0.0
        damt = m["amt_yi"] - prev_s["amt_yi"] if prev_s else 0.0
        if is_etf(sym) and m["amt_yi"] >= 0.5:
            if dpct >= 1.0 and m["last"] >= m["vwap"]:
                buckets["etf_up"].append(
                    f"- **{sym} {m['name']}**：{m['last']:.3f} ({m['pct']:+.2f}%) | 5m **+{dpct:.2f}%** | 均 {m['vwap']:.3f} | 额 {m['amt_yi']:.2f}亿"
                )
            elif dpct <= -1.0 and m["last"] < m["vwap"]:
                buckets["etf_down"].append(
                    f"- **{sym} {m['name']}**：{m['last']:.3f} ({m['pct']:+.2f}%) | 5m **{dpct:.2f}%** | 均 {m['vwap']:.3f} | 额 {m['amt_yi']:.2f}亿"
                )
            continue
        if not is_a_share(sym):
            continue
        info = l2_map.get(sym[:6], {})
        sec = info.get("sector", "")
        if dpct >= 2.5 and damt >= 0.30 and m["last"] >= m["vwap"]:
            buckets["stock_up"].append(
                f"- **{sym} {m['name']}**：{m['last']:.2f} ({m['pct']:+.2f}%) | 5m急涨 **+{dpct:.2f}%** (+{damt:.2f}亿) | 均 {m['vwap']:.2f}"
            )
            if sec:
                sector_surges[sec].append(f"{sym} {m['name']}")
        elif dpct <= -2.5 and m["amt_yi"] >= 1.0:
            buckets["stock_down"].append(
                f"- **{sym} {m['name']}**：{m['last']:.2f} ({m['pct']:+.2f}%) | 5m急跌 **{dpct:.2f}%** | 均 {m['vwap']:.2f} ⚠️"
            )
        _check_wyckoff_signals(sym, m, dpct, damt, info, red_ratio, cooldowns, now_ts, buckets)
    for sec, members in sector_surges.items():
        if len(members) >= 3 and _check_cooldown(cooldowns, f"L2:sec:{sec}", now_ts):
            buckets["l2_sector"].append(
                f"- 🟡 **【{sec} 板块5m共振】** {len(members)} 只个股同时急涨：{', '.join(members[:4])}"
            )
    return buckets


def _check_wyckoff_signals(
    sym: str,
    m: dict[str, Any],
    dpct: float,
    damt: float,
    info: dict[str, Any],
    red_ratio: float,
    cooldowns: dict[str, float],
    now_ts: float,
    buckets: dict[str, list[str]],
) -> None:
    if not info.get("l2_eligible"):
        return
    chans = "+".join(info.get("channels", []))
    prev_amt = float(info.get("prev_amt_yi") or 0.0)
    vol_thresh = max(prev_amt * 0.12, 0.25)
    if (
        info.get("is_springboard")
        and red_ratio > 0.80
        and dpct >= 2.5
        and 2.8 <= m["pct"] <= 6.0
        and 0.0 <= m["vwap_dev"] <= 2.0
        and damt >= vol_thresh
        and _check_cooldown(cooldowns, f"L4:{sym}", now_ts)
    ):
        buckets["l4"].append(
            f"🔴 **【L4 满级起跳板突破】{sym} {m['name']}** [{info.get('sector', '')}]：{m['last']:.2f} ({m['pct']:+.2f}%) | 5m +{dpct:.2f}% (+{damt:.2f}亿) | 均 {m['vwap']:.2f}"
        )
        return
    if (
        ("地量" in chans or "吸筹" in chans)
        and dpct >= 1.8
        and 2.8 <= m["pct"] <= 5.0
        and 0.0 <= m["vwap_dev"] <= 2.0
        and damt >= vol_thresh
        and _check_cooldown(cooldowns, f"L3:{sym}", now_ts)
    ):
        buckets["l3"].append(
            f"- **{sym} {m['name']}** [{info.get('sector', '')}]：**{m['last']:.2f} ({m['pct']:+.2f}%)** | "
            f"5m **+{dpct:.2f}%** (+{damt:.2f}亿) | 均 {m['vwap']:.2f} ✅ | {chans} RPS={info.get('rps', 0):.0f}"
        )


def analyze_snapshot_diff(
    curr_quotes: dict[str, dict[str, Any]],
    prev_state: dict[str, Any],
    l2_map: dict[str, dict[str, Any]],
    holdings: dict[str, dict[str, float]],
    now_ts: float | None = None,
) -> tuple[str, str, str, int, dict[str, float]]:
    ts = now_ts if now_ts is not None else time.time()
    today_str = cn_today().isoformat()
    has_baseline = prev_state.get("date") == today_str and bool(prev_state.get("quotes"))
    prev_q = prev_state.get("quotes", {}) if has_baseline else {}
    cooldowns: dict[str, float] = dict(prev_state.get("cooldowns", {})) if has_baseline else {}

    sh = curr_quotes.get("000001.SH", {})
    sz = curr_quotes.get("399001.SZ", {})
    tot_amt_yi = (float(sh.get("amount") or 0.0) + float(sz.get("amount") or 0.0)) / 1e8
    sh_pct = (compute_quote_metrics(sh) or {}).get("pct", 0.0)
    sz_pct = (compute_quote_metrics(sz) or {}).get("pct", 0.0)

    red_count, green_count = 0, 0
    for sym, q in curr_quotes.items():
        if not is_a_share(sym):
            continue
        m = compute_quote_metrics(q)
        if m:
            red_count += int(m["pct"] > 0)
            green_count += int(m["pct"] < 0)
    total_active = max(red_count + green_count, 1)
    red_ratio = red_count / total_active
    d_red = (red_count - int(prev_state.get("red", red_count))) if has_baseline else 0

    h_lines, h_l3, h_l4 = _evaluate_holdings(curr_quotes, prev_q, has_baseline, holdings, cooldowns, ts)
    buckets = _scan_equities_and_etfs(curr_quotes, prev_q, has_baseline, l2_map, red_ratio, cooldowns, ts)
    buckets["l3"] = h_l3 + buckets["l3"]
    buckets["l4"] = h_l4 + buckets["l4"]

    if abs(d_red) >= 400 and _check_cooldown(cooldowns, "L2:breadth", ts):
        buckets["l2_sector"].insert(
            0,
            f"- 🟡 **【市场情绪5m骤变】** 红盘家数 5 分钟变化 **{d_red:+d} 家**（当前红 {red_count} / 绿 {green_count}）",
        )

    level = _determine_alert_level(buckets, h_lines)
    title, body = _format_card(level, tot_amt_yi, sh_pct, sz_pct, red_count, green_count, d_red, h_lines, buckets)
    return level, title, body, red_count, cooldowns


def _determine_alert_level(buckets: dict[str, list[str]], h_lines: list[str]) -> str:
    if buckets["l4"]:
        return "L4"
    if buckets["l3"]:
        return "L3"
    if (
        buckets["l2_sector"]
        or buckets["etf_up"]
        or buckets["etf_down"]
        or len(buckets["stock_up"]) >= 3
        or len(buckets["stock_down"]) >= 3
        or any("🟡" in line for line in h_lines)
    ):
        return "L2"
    return "L1"


def _format_card(
    level: str,
    tot_amt_yi: float,
    sh_pct: float,
    sz_pct: float,
    red_count: int,
    green_count: int,
    d_red: int,
    h_lines: list[str],
    buckets: dict[str, list[str]],
) -> tuple[str, str]:
    now_hm = datetime.now(CN_TZ).strftime("%H:%M")
    title = f"【{level}】{now_hm} 5m全市场雷达：两市{tot_amt_yi:.0f}亿 | 红{red_count}/绿{green_count} ({d_red:+d})"
    secs = [
        f"全市场 7,807 标的 5 分钟扫描完成：两市成交 {tot_amt_yi:.0f} 亿 | 上证 {sh_pct:+.2f}% 深成指 {sz_pct:+.2f}% | "
        f"上涨 {red_count} 家 / 下跌 {green_count} 家（5m变化 {d_red:+d} 家）。"
    ]
    if h_lines:
        secs.append("**1. 持仓5分钟贴身监控**\n" + "\n".join(h_lines))
    if buckets["l4"]:
        secs.append("**2. 🔴 L4 电话级警报**\n" + "\n".join(buckets["l4"][:5]))
    if buckets["l3"]:
        secs.append("**3. 🟠 L3 短信级：威科夫 L2 地量/吸筹 5m 倍量点火 & 破位预警**\n" + "\n".join(buckets["l3"][:8]))
    if buckets["l2_sector"]:
        secs.append("**4. 🟡 L2 市场情绪与行业共振**\n" + "\n".join(buckets["l2_sector"][:6]))
    if buckets["etf_up"] or buckets["etf_down"]:
        secs.append(
            "**5. 🟡 全市场 1,677 只 ETF 5m 急涨/急跌**\n" + "\n".join(buckets["etf_up"][:6] + buckets["etf_down"][:5])
        )
    if buckets["stock_up"] or buckets["stock_down"]:
        secs.append(
            "**6. 🟡 全市场 5,578 只 A 股 5m 火箭发射 / 高台跳水**\n"
            + "\n".join(buckets["stock_up"][:6] + buckets["stock_down"][:6])
        )
    secs.append("⚠️ 风险提醒: 严守分时均价线（VWAP）与今日真实量比双确认，绝不在均线下方接飞刀。")
    return title, "\n\n".join(secs)


def should_send_card(level: str, now_bjt: datetime, force_send: bool, urgent_only: bool) -> bool:
    if urgent_only:
        return level in ("L2", "L3", "L4")
    if force_send or level in ("L2", "L3", "L4"):
        return True
    hm = (now_bjt.hour, now_bjt.minute)
    return now_bjt.minute in (0, 30) or hm in ((13, 15), (14, 45))


def send_feishu_tiered_alert(
    title: str, content: str, level: str = "L1", user_open_id: str = "", dry_run: bool = False
) -> str | None:
    if dry_run:
        print(f"[DRY-RUN {level}] {title}\n{content}")
        return "dry_run_msg_id"
    if not user_open_id:
        print(f"[SKIP-FEISHU {level}] No feishu_user_open_id configured. {title}")
        return None
    card_cmd = [
        sys.executable,
        str(REPO_ROOT / "integrations" / "feishu_notifier.py"),
        "--title",
        title,
        "--content",
        content,
        "--receive-id-type",
        "open_id",
        "--receive-id",
        user_open_id,
    ]
    res = subprocess.run(card_cmd, capture_output=True, text=True, check=False)
    msg_id = None
    for line in res.stdout.splitlines():
        if "message_id=" in line:
            msg_id = line.split("message_id=")[-1].strip().rstrip(")")
            break
    if not msg_id:
        return None
    urgent_map = {"L2": "urgent_app", "L3": "urgent_sms", "L4": "urgent_phone"}
    if level in urgent_map:
        subprocess.run(
            [
                "lark-cli",
                "im",
                "messages",
                urgent_map[level],
                "--as",
                "bot",
                "--params",
                json.dumps({"message_id": msg_id, "user_id_type": "open_id"}, ensure_ascii=False),
                "--data",
                json.dumps({"user_id_list": [user_open_id]}, ensure_ascii=False),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    return msg_id


def run_once(args: argparse.Namespace) -> None:
    now_bjt = datetime.now(CN_TZ)
    if not args.ignore_hours and not args.dry_run and not is_cn_trading_session(now_bjt):
        print(f"[{now_bjt.strftime('%Y-%m-%d %H:%M:%S')}] Outside A-share trading hours, skipping.")
        return
    load_env_file()
    cfg = load_radar_config(args.config)
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=os.environ["TICKFLOW_API_KEY"])
    l2_map = load_wyckoff_l2_map(args.trace, cfg["trace_dirs"])
    prev_state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    curr_quotes = client.get_quotes(universes=["CN_Equity_A", "CN_ETF", "CN_Index"])
    level, title, content, red_count, cooldowns = analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, cfg["holdings"]
    )
    compact_snaps = {}
    for sym, q in curr_quotes.items():
        m = compute_quote_metrics(q)
        if m:
            compact_snaps[sym] = {"pct": round(m["pct"], 3), "amt_yi": round(m["amt_yi"], 3)}
    STATE_PATH.write_text(
        json.dumps(
            {
                "date": cn_today().isoformat(),
                "ts": time.time(),
                "red": red_count,
                "cooldowns": cooldowns,
                "quotes": compact_snaps,
            }
        ),
        encoding="utf-8",
    )
    if should_send_card(level, now_bjt, args.force_send, args.urgent_only):
        msg_id = send_feishu_tiered_alert(
            title, content, level=level, user_open_id=cfg["feishu_user_open_id"], dry_run=args.dry_run
        )
        print(f"[{now_bjt.strftime('%H:%M:%S')}] Sent {level} alert: msg_id={msg_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="5-minute full-market intraday radar (09:30-15:00 BJT)")
    parser.add_argument("--once", action="store_true", help="Run a single 5-min scan")
    parser.add_argument("--daemon", action="store_true", help="Run every 5 minutes during trading hours")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval seconds (default 300 = 5m)")
    parser.add_argument("--config", type=str, default=None, help="Path to local radar config JSON")
    parser.add_argument("--trace", type=str, default=None, help="Path to review_trace_*.json.gz")
    parser.add_argument("--urgent-only", action="store_true", help="Only send Feishu card when L2/L3/L4 triggers")
    parser.add_argument("--force-send", action="store_true", help="Always send Feishu card on every scan")
    parser.add_argument("--ignore-hours", action="store_true", help="Run scan even outside trading hours")
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
