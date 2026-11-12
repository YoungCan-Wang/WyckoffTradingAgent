#!/usr/bin/env python3
"""Full-market 5-minute intraday radar (09:30-15:00 BJT, 7,807 instruments) with 4-tier Feishu alerts."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.candidate_metadata import code6 as _code6  # noqa: E402
from utils.feishu_report_card import build_report_card_elements, report_card_template  # noqa: E402
from utils.trading_clock import CN_TZ, cn_today, is_a_share_trading_day  # noqa: E402

STATE_PATH = Path("/tmp/wyckoff_intraday_radar_state.json")
ALERT_LOG_PATH = Path.home() / ".wyckoff" / "radar_alerts.jsonl"
TURNOVER_DIR = Path.home() / ".wyckoff" / "intraday_turnover"
_LAST_ALERT_RECORDS: list[dict[str, Any]] = []
COOLDOWN_SECONDS = 1800
HOLDING_STOP_COOLDOWN_SECONDS = 86400
L1_INTERVAL_SECONDS = 1740
MAX_BASELINE_GAP_SECONDS = 900
MAX_TRACE_AGE_DAYS = 3
MIN_BREADTH_COVERAGE_RATIO = 0.90


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


def resolve_lark_open_id(explicit_id: str | None = None) -> str:
    if explicit_id and explicit_id.strip():
        return explicit_id.strip()
    env_id = os.environ.get("FEISHU_USER_OPEN_ID", "").strip()
    if env_id:
        return env_id
    res = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True, check=False)
    if res.returncode == 0 and res.stdout.strip():
        try:
            data = json.loads(res.stdout)
            return str(((data.get("identities") or {}).get("user") or {}).get("openId") or "").strip()
        except Exception:
            return ""
    return ""


def _normalize_holding_symbol(raw: str) -> str:
    """Map portfolio 6-digit codes to TickFlow quote keys (.SH/.SZ/.BJ)."""
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    if "." in s and len(s.split(".", 1)[0]) == 6:
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) != 6:
        return s
    if digits.startswith(("0", "1", "2", "3")):
        return f"{digits}.SZ"
    if digits.startswith(("4", "8", "9")):
        return f"{digits}.BJ"
    return f"{digits}.SH"


def parse_holdings_arg(raw_list: list[str] | None) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for item in raw_list or []:
        parts = [p.strip() for p in item.split(":") if p.strip()]
        if len(parts) >= 3:
            sym = _normalize_holding_symbol(parts[0]) or parts[0]
            out[sym] = {"cost": float(parts[1]), "stop": float(parts[2])}
    return out


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


def _parse_trace_date(latest: Path, data: dict[str, Any]) -> date | None:
    raw = str(data.get("trade_date") or "").strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        stem = latest.name.replace("review_trace_", "").split(".")[0].replace("-", "")
        raw = stem if len(stem) == 8 and stem.isdigit() else ""
    if not raw:
        return None
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))


def _trading_days_since(trace_date: date, today: date) -> int:
    if today <= trace_date:
        return 0
    if (today - trace_date).days > 25:
        return (today - trace_date).days
    cur = trace_date
    count = 0
    while cur < today:
        cur = cur + timedelta(days=1)
        if is_a_share_trading_day(cur):
            count += 1
    return count


def _load_sibling_springboards(trace_file: Path, trace_date: date | None) -> set[str]:
    springboards: set[str] = set()
    if trace_date is None:
        return springboards
    ymd = trace_date.strftime("%Y%m%d")
    rec_file = trace_file.parent / f"recommendation_tracking_{ymd}.json"
    if not rec_file.exists():
        return springboards
    try:
        payload = json.loads(rec_file.read_text(encoding="utf-8"))
        rec_date = str(payload.get("recommend_date") or ymd).replace("-", "")
        if rec_date != ymd:
            return springboards
        for row in payload.get("rows", []):
            if row.get("springboard_combo") == "A+B+C" or row.get("springboard_grade") == "A+B+C":
                normalized = _code6(row.get("code"))
                if normalized:
                    springboards.add(normalized)
    except Exception:
        return springboards
    return springboards


def _collect_trace_candidates(trace_path: str | None, trace_dirs: list[str] | None) -> list[Path]:
    if trace_path:
        p = Path(trace_path).expanduser()
        return [p] if p.exists() else []
    candidates: list[Path] = []
    for raw_dir in trace_dirs or [str(Path.home() / ".wyckoff" / "traces"), "/tmp", str(REPO_ROOT / "data")]:
        base = Path(raw_dir).expanduser()
        if base.exists():
            candidates.extend(base.rglob("review_trace_*.json.gz"))
    return sorted(candidates, key=lambda p: p.name)


def _auto_fetch_latest_trace(target_dir: Path) -> bool:
    try:
        res = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "wyckoff_funnel.yml",
                "--status",
                "success",
                "-L",
                "1",
                "--json",
                "databaseId",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return False
        runs = json.loads(res.stdout)
        run_id = str((runs[0] if runs else {}).get("databaseId") or "").strip()
        if not run_id:
            return False
        target_dir.mkdir(parents=True, exist_ok=True)
        marker = target_dir / f".fetched_run_{run_id}"
        if marker.exists():
            return False
        dl = subprocess.run(
            ["gh", "run", "download", run_id, "-p", "daily-job-artifacts-*", "-D", str(target_dir)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if dl.returncode == 0:
            marker.write_text(run_id, encoding="utf-8")
            return True
    except Exception:
        return False
    return False


def _parse_trace_symbols(data: dict[str, Any], springboards: set[str]) -> dict[str, dict[str, Any]]:
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
        code = _code6(raw_key or s.get("symbol", ""))
        if not code:
            continue
        raw_ch = s.get("l2_channels") or s.get("l2_channel") or []
        chans = (
            [c.strip() for c in raw_ch.split("+") if c.strip()]
            if isinstance(raw_ch, str)
            else [str(c).strip() for c in raw_ch if str(c).strip()]
        )
        entry_obj = s.get("entry") if isinstance(s.get("entry"), dict) else {}
        entry_type = str(entry_obj.get("entry_type") or "")
        stage = str(s.get("stage") or "")
        is_sb = (
            code in springboards
            or entry_type == "launchpad"
            or "启动平台" in str(s.get("reason") or "")
            or bool(s.get("is_springboard"))
        )
        out[code] = {
            "name": s.get("name", ""),
            "sector": s.get("sector", ""),
            "rps": float(s.get("rps_fast") or 0.0),
            "l2_eligible": bool(s.get("l2_eligible")),
            "l3_eligible": bool(s.get("l3_eligible")),
            "is_candidate": stage == "候选池已捕获",
            "channels": chans,
            "is_springboard": is_sb,
        }
    return out


def load_wyckoff_l2_map(
    trace_path: str | None = None,
    trace_dirs: list[str] | None = None,
    max_age_days: int | None = MAX_TRACE_AGE_DAYS,
    auto_fetch: bool | None = None,
) -> dict[str, dict[str, Any]]:
    do_fetch = (trace_path is None and trace_dirs is None) if auto_fetch is None else auto_fetch
    default_trace_dir = Path(trace_dirs[0]).expanduser() if trace_dirs else Path.home() / ".wyckoff" / "traces"
    candidates = _collect_trace_candidates(trace_path, trace_dirs)
    if not candidates and do_fetch and _auto_fetch_latest_trace(default_trace_dir):
        candidates = _collect_trace_candidates(trace_path, trace_dirs)
    if not candidates:
        print(
            "[WARN] No review_trace_*.json.gz found; Wyckoff L3/L4 and sector resonance are disabled.",
            file=sys.stderr,
        )
        return {}
    latest = candidates[-1]
    with gzip.open(latest, "rt", encoding="utf-8") as f:
        data = json.load(f)
    t_date = _parse_trace_date(latest, data)
    if max_age_days is not None and t_date is not None and _trading_days_since(t_date, cn_today()) > max_age_days:
        if do_fetch and _auto_fetch_latest_trace(default_trace_dir):
            candidates = _collect_trace_candidates(trace_path, trace_dirs)
            latest = candidates[-1]
            with gzip.open(latest, "rt", encoding="utf-8") as f:
                data = json.load(f)
            t_date = _parse_trace_date(latest, data)
        if t_date is not None and _trading_days_since(t_date, cn_today()) > max_age_days:
            print(
                f"[WARN] Ignoring stale review_trace {latest.name} (trade_date={t_date}); "
                "Wyckoff L3/L4 and sector resonance are disabled.",
                file=sys.stderr,
            )
            return {}
    springboards = _load_sibling_springboards(latest, t_date)
    return _parse_trace_symbols(data, springboards)


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
    high_p = max(float(q.get("high") or last_p), last_p)
    low_p = min(float(q.get("low") or last_p), last_p) if float(q.get("low") or 0.0) > 0 else last_p
    open_p = float(q.get("open") or prev_c)
    upper_wick_pct = (high_p - last_p) / high_p * 100.0 if high_p > 0 else 0.0
    amp_pct = (high_p - low_p) / prev_c * 100.0 if prev_c > 0 else 0.0
    open_pct = (open_p / prev_c - 1.0) * 100.0 if prev_c > 0 else 0.0
    return {
        "symbol": q.get("symbol", ""),
        "name": name,
        "last": last_p,
        "prev_close": prev_c,
        "pct": pct,
        "amt_yi": amt / 1e8,
        "vwap": vwap,
        "vwap_dev": vwap_dev,
        "upper_wick_pct": upper_wick_pct,
        "amp_pct": amp_pct,
        "open_pct": open_pct,
    }


def _check_cooldown(cooldowns: dict[str, float], key: str, now_ts: float, window: int = COOLDOWN_SECONDS) -> bool:
    if key in cooldowns and now_ts - float(cooldowns[key]) < window:
        return False
    cooldowns[key] = now_ts
    return True


def _is_fresh_baseline(prev_state: dict[str, Any], today_str: str, now_ts: float) -> bool:
    if prev_state.get("date") != today_str or not prev_state.get("quotes"):
        return False
    prev_ts = float(prev_state.get("ts") or 0.0)
    if prev_ts <= 0 or now_ts < prev_ts:
        return False
    gap = now_ts - prev_ts
    if gap <= MAX_BASELINE_GAP_SECONDS:
        return True
    prev_dt = datetime.fromtimestamp(prev_ts, tz=CN_TZ)
    curr_dt = datetime.fromtimestamp(now_ts, tz=CN_TZ)
    is_lunch_bridge = (11, 20) <= (prev_dt.hour, prev_dt.minute) <= (11, 36) and (12, 55) <= (
        curr_dt.hour,
        curr_dt.minute,
    ) <= (13, 12)
    return is_lunch_bridge and gap <= 6300


def holdings_missing_quote_symbols(
    holdings: dict[str, dict[str, float]],
    curr_quotes: dict[str, dict[str, Any]],
) -> list[str]:
    """Holdings with no usable TickFlow row this frame (stop/plunge would be skipped)."""
    return [sym for sym in holdings if not compute_quote_metrics(curr_quotes.get(sym, {}))]


def _format_holding_line(sym: str, m: dict[str, Any], dpct: float, cfg: dict[str, float], tag: str) -> str:
    cost = float(cfg.get("cost", 0.0))
    cost_part = f" | 成本 {cost:.2f} ({(m['last'] / cost - 1.0) * 100.0:+.2f}%)" if cost > 0 else ""
    vwap_status = "🟢均线上" if m["last"] >= m["vwap"] else "🔴均线水下"
    return (
        f"- **{sym} {m['name']}**：现价 **{m['last']:.2f} ({m['pct']:+.2f}%)**{cost_part} | "
        f"均价 {m['vwap']:.2f} ({vwap_status} {m['vwap_dev']:+.2f}%) | 5m涨速 {dpct:+.2f}% | 额 {m['amt_yi']:.2f}亿{tag}"
    )


def _evaluate_holdings(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]],
    has_baseline: bool,
    holdings: dict[str, dict[str, float]],
    cooldowns: dict[str, float],
    now_ts: float,
    alert_records: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], list[str]]:
    lines: list[str] = []
    l2_alerts: list[str] = []
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
        if stop > 0 and m["last"] <= stop:
            tag = " 🔴破止损!"
            # Kind-scoped key: must not share L4:{sym} with springboard breakout (86400s would mute stop phone).
            if _check_cooldown(cooldowns, f"L4:holding_stop:{sym}", now_ts, window=HOLDING_STOP_COOLDOWN_SECONDS):
                l4_alerts.append(f"🔴 **【L4 破位止损】{sym} {m['name']}** 跌破止损 {stop}（现价 {m['last']:.2f}）")
                alert_records.append(
                    {
                        "ts": now_ts,
                        "level": "L4",
                        "kind": "holding_stop",
                        "symbol": sym,
                        "last": m["last"],
                        "stop": stop,
                    }
                )
        elif dpct <= -2.0 and m["last"] < m["vwap"]:
            tag = " 🟠急跌!"
            if _check_cooldown(cooldowns, f"L3:holding_plunge:{sym}", now_ts):
                l3_alerts.append(f"🟠 **【持仓急跌】{sym} {m['name']}** 5m 急跌 {dpct:.2f}% 且跌破均线 {m['vwap']:.2f}")
                alert_records.append(
                    {
                        "ts": now_ts,
                        "level": "L3",
                        "kind": "holding_plunge",
                        "symbol": sym,
                        "last": m["last"],
                        "dpct_5m": round(dpct, 2),
                    }
                )
        elif m["vwap_dev"] > 3.0:
            tag = " 🟡偏离均线>3%可做T高抛"
            if _check_cooldown(cooldowns, f"L2:holding_t0:{sym}", now_ts):
                l2_alerts.append(
                    f"- 🟡 **【持仓做T高抛】{sym} {m['name']}** 现价 {m['last']:.2f} 偏离均线 +{m['vwap_dev']:.2f}%"
                )
                alert_records.append(
                    {
                        "ts": now_ts,
                        "level": "L2",
                        "kind": "holding_t0_high",
                        "symbol": sym,
                        "last": m["last"],
                        "vwap_dev": round(m["vwap_dev"], 2),
                    }
                )
        lines.append(_format_holding_line(sym, m, dpct, cfg, tag))
    return lines, l2_alerts, l3_alerts, l4_alerts


def _scan_equities_and_etfs(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]],
    has_baseline: bool,
    l2_map: dict[str, dict[str, Any]],
    red_ratio: float,
    coverage_ok: bool,
    cooldowns: dict[str, float],
    now_ts: float,
    alert_records: list[dict[str, Any]],
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    sector_surges: dict[str, list[str]] = defaultdict(list)
    sector_leaders: dict[str, list[tuple[float, str]]] = defaultdict(list)
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
        chans = "+".join(info.get("channels", []))
        rps_v = float(info.get("rps") or 0.0)
        if (
            sec
            and info.get("l2_eligible")
            and (info.get("l3_eligible") or info.get("is_candidate"))
            and ("地量" in chans or "吸筹" in chans)
            and 12.0 <= rps_v <= 80.0
            and 1.5 <= m["pct"] <= 4.8
            and 0.0 <= m["vwap_dev"] <= 2.0
            and m["amt_yi"] >= 1.5
            and m.get("upper_wick_pct", 0.0) <= 1.2
        ):
            sector_leaders[sec].append(
                (m["amt_yi"], f"`{sym[:6]}` {m['name']}({m['last']:.2f} {m['pct']:+.1f}%·{chans})")
            )
        if dpct >= 2.5 and damt >= 0.30 and m["last"] >= m["vwap"]:
            is_steady = (
                bool(info.get("l2_eligible")) and rps_v <= 80.0 and m["pct"] <= 5.0 and m.get("amp_pct", 0.0) <= 6.5
            )
            style_tag = "🟢L2稳健" if is_steady else "⚠️妖股/高位勿追"
            buckets["stock_up"].append(
                f"- **{sym} {m['name']}** [{style_tag}]：{m['last']:.2f} ({m['pct']:+.2f}%) | 5m急涨 **+{dpct:.2f}%** (+{damt:.2f}亿) | 均 {m['vwap']:.2f}"
            )
            if sec:
                sector_surges[sec].append(f"{sym} {m['name']}")
        elif dpct <= -2.5 and m["amt_yi"] >= 1.0:
            buckets["stock_down"].append(
                f"- **{sym} {m['name']}**：{m['last']:.2f} ({m['pct']:+.2f}%) | 5m急跌 **{dpct:.2f}%** | 均 {m['vwap']:.2f} ⚠️"
            )
        _check_wyckoff_signals(
            sym, m, dpct, damt, info, red_ratio, coverage_ok, cooldowns, now_ts, buckets, alert_records
        )
    _prepend_sector_resonances(sector_surges, sector_leaders, l2_map, cooldowns, now_ts, buckets, alert_records)
    return buckets


def _prepend_sector_resonances(
    sector_surges: dict[str, list[str]],
    sector_leaders: dict[str, list[tuple[float, str]]],
    l2_map: dict[str, dict[str, Any]],
    cooldowns: dict[str, float],
    now_ts: float,
    buckets: dict[str, list[str]],
    alert_records: list[dict[str, Any]],
) -> None:
    sec_totals: dict[str, int] = defaultdict(int)
    for meta in l2_map.values():
        s_name = str(meta.get("sector") or "")
        if s_name:
            sec_totals[s_name] += 1
    ranked_surges = sorted(
        sector_surges.items(),
        key=lambda kv: (len(kv[1]) / max(sec_totals.get(kv[0], 30), 10), len(kv[1])),
        reverse=True,
    )
    sector_events: list[str] = []
    for sec, members in ranked_surges:
        if len(members) >= 3 and _check_cooldown(cooldowns, f"L2:sec:{sec}", now_ts):
            top_l2 = [x[1] for x in sorted(sector_leaders.get(sec, []), key=lambda t: -t[0])[:2]]
            l2_suffix = f" 👉 🎯同板块L2稳健中军：{'、'.join(top_l2)}" if top_l2 else ""
            sector_events.append(
                f"- 🟡 **【{sec} 板块5m共振】** {len(members)} 只个股同时急涨：{', '.join(members[:4])}{l2_suffix}"
            )
            alert_records.append(
                {"ts": now_ts, "level": "L2", "kind": "sector_resonance", "sector": sec, "count": len(members)}
            )
    if sector_events:
        buckets["l2_events"] = sector_events + buckets["l2_events"]


def _check_wyckoff_signals(
    sym: str,
    m: dict[str, Any],
    dpct: float,
    damt: float,
    info: dict[str, Any],
    red_ratio: float,
    coverage_ok: bool,
    cooldowns: dict[str, float],
    now_ts: float,
    buckets: dict[str, list[str]],
    alert_records: list[dict[str, Any]],
) -> None:
    # L4 springboard is identity-based (A+B+C / launchpad); must NOT be gated by l2_eligible or upper_wick_pct.
    chans = "+".join(info.get("channels", []))
    if (
        info.get("is_springboard")
        and coverage_ok
        and red_ratio > 0.80
        and dpct >= 2.5
        and 2.8 <= m["pct"] <= 6.0
        and 0.0 <= m["vwap_dev"] <= 2.0
        and damt >= 0.30
    ):
        if _check_cooldown(cooldowns, f"L4:springboard:{sym}", now_ts):
            buckets["l4"].append(
                f"🔴 **【L4 满级起跳板突破】{sym} {m['name']}** [{info.get('sector', '')}]：{m['last']:.2f} ({m['pct']:+.2f}%) | 5m +{dpct:.2f}% (+{damt:.2f}亿) | 均 {m['vwap']:.2f}"
            )
            alert_records.append(
                {
                    "ts": now_ts,
                    "level": "L4",
                    "kind": "springboard_breakout",
                    "symbol": sym,
                    "name": m["name"],
                    "sector": info.get("sector", ""),
                    "last": round(m["last"], 3),
                    "pct": round(m["pct"], 2),
                    "dpct_5m": round(dpct, 2),
                    "vwap": round(m["vwap"], 3),
                    "vwap_dev": round(m["vwap_dev"], 2),
                    "amt_yi": round(m["amt_yi"], 2),
                }
            )
        return
    _check_wyckoff_ignition(sym, m, dpct, damt, info, chans, cooldowns, now_ts, buckets, alert_records)


def _check_wyckoff_ignition(
    sym: str,
    m: dict[str, Any],
    dpct: float,
    damt: float,
    info: dict[str, Any],
    chans: str,
    cooldowns: dict[str, float],
    now_ts: float,
    buckets: dict[str, list[str]],
    alert_records: list[dict[str, Any]],
) -> None:
    if not info.get("l2_eligible") or float(m.get("upper_wick_pct", 0.0)) > 2.0:
        return
    is_core_pool = bool(info.get("l3_eligible") or info.get("is_candidate"))
    rps_val = float(info.get("rps") or 0.0)
    now_hm = datetime.fromtimestamp(now_ts, tz=CN_TZ).strftime("%H:%M")
    is_steady_push = (
        now_hm >= "10:30"
        and 24.0 <= rps_val <= 68.0
        and float(m.get("upper_wick_pct", 0.0)) <= 1.0
        and float(m.get("amp_pct", 0.0)) <= 5.5
        and dpct >= 1.2
        and damt >= 0.25
        and m["amt_yi"] >= 1.8
    )
    is_standard_ign = dpct >= 1.8 and damt >= 0.30 and m["amt_yi"] >= 1.0
    if not (
        is_core_pool
        and ("地量" in chans or "吸筹" in chans)
        and rps_val <= 80.0
        and float(m.get("amp_pct", 0.0)) <= 6.5
        and float(m.get("open_pct", 0.0)) >= -2.0
        and (is_standard_ign or is_steady_push)
        and 2.8 <= m["pct"] <= 5.0
        and 0.0 <= m["vwap_dev"] <= 2.0
    ):
        return
    if now_hm < "10:30":
        if _check_cooldown(cooldowns, f"L1:pre1030_obs:{sym}", now_ts):
            buckets["l1_obs"].append(
                f"- **{sym} {m['name']}** [{info.get('sector', '')}]：**{m['last']:.2f} ({m['pct']:+.2f}%)** | "
                f"5m **+{dpct:.2f}%** (+{damt:.2f}亿) | 均 {m['vwap']:.2f} ⏳[待10:30验钞观察] | {chans} RPS={rps_val:.0f}"
            )
        return
    if _check_cooldown(cooldowns, f"L3:wyckoff_ignition:{sym}", now_ts):
        mode_tag = "🎯稳健中军加速" if (is_steady_push and not is_standard_ign) else "✅"
        buckets["l3"].append(
            f"- **{sym} {m['name']}** [{info.get('sector', '')}]：**{m['last']:.2f} ({m['pct']:+.2f}%)** | "
            f"5m **+{dpct:.2f}%** (+{damt:.2f}亿) | 均 {m['vwap']:.2f} {mode_tag} | {chans} RPS={rps_val:.0f}"
        )
        alert_records.append(
            {
                "ts": now_ts,
                "level": "L3",
                "kind": "wyckoff_l2l3_ignition",
                "symbol": sym,
                "name": m["name"],
                "sector": info.get("sector", ""),
                "last": round(m["last"], 3),
                "pct": round(m["pct"], 2),
                "dpct_5m": round(dpct, 2),
                "vwap": round(m["vwap"], 3),
                "vwap_dev": round(m["vwap_dev"], 2),
                "amt_yi": round(m["amt_yi"], 2),
            }
        )


def _hm_to_minutes(hm: str) -> int:
    parts = hm.split(":")
    return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() else -1


def _normalize_market_hm(hm: str) -> str:
    if "11:30" < hm < "12:59":
        return "11:30"
    if hm > "15:00":
        return "15:00"
    return hm


def _read_turnover_points(path: Path) -> dict[str, float]:
    points: dict[str, float] = {}
    if not path.exists():
        return points
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            points[str(row["hm"])] = float(row["tot_amt_yi"])
        except Exception:
            continue
    return points


def update_and_compare_turnover(
    today_date: date,
    now_hm: str,
    tot_amt_yi: float,
    index_ok: bool,
    turnover_dir: Path | None = None,
) -> str:
    if not index_ok or tot_amt_yi <= 0:
        return "两市成交：数据缺口（缺少上证或深成指完整报价）"
    eff_hm = _normalize_market_hm(now_hm)
    base_dir = turnover_dir or TURNOVER_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    today_file = base_dir / f"turnover_{today_date.strftime('%Y%m%d')}.jsonl"
    today_pts = _read_turnover_points(today_file)
    if eff_hm not in today_pts:
        with open(today_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"hm": eff_hm, "tot_amt_yi": round(tot_amt_yi, 2)}, ensure_ascii=False) + "\n")
        today_pts[eff_hm] = tot_amt_yi
    pm_part = ""
    if eff_hm >= "13:00":
        am_candidates = [v for k, v in today_pts.items() if "11:25" <= k < "13:00"]
        if am_candidates:
            pm_part = f" | 13:00至今 {max(0.0, tot_amt_yi - am_candidates[-1]):.0f}亿"
    prev_files = sorted(p for p in base_dir.glob("turnover_*.jsonl") if p.name < today_file.name)
    if not prev_files:
        return f"两市成交 {tot_amt_yi:.0f} 亿（同时刻对比：无基准）{pm_part}"
    prev_file = prev_files[-1]
    raw_ymd = prev_file.stem.replace("turnover_", "")
    bench_date = f"{raw_ymd[:4]}-{raw_ymd[4:6]}-{raw_ymd[6:8]}" if len(raw_ymd) == 8 and raw_ymd.isdigit() else raw_ymd
    prev_pts = _read_turnover_points(prev_file)
    now_min = _hm_to_minutes(eff_hm)
    best_hm, best_gap = "", 999
    for k in prev_pts:
        gap = abs(_hm_to_minutes(k) - now_min)
        if gap <= 10 and gap < best_gap:
            best_hm, best_gap = k, gap
    if not best_hm or prev_pts[best_hm] <= 0:
        return f"两市成交 {tot_amt_yi:.0f} 亿（同时刻对比：无基准）{pm_part}"
    prev_amt = prev_pts[best_hm]
    diff_pct = (tot_amt_yi / prev_amt - 1.0) * 100.0
    return f"两市成交 {tot_amt_yi:.0f} 亿（较 {bench_date} {best_hm} {prev_amt:.0f}亿 {diff_pct:+.1f}%）{pm_part}"


def append_alert_records(
    records: list[dict[str, Any]],
    log_path: Path | None = None,
    sent: bool = True,
    msg_id: str | None = None,
) -> None:
    if not records:
        return
    target = log_path or ALERT_LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        today_str = cn_today().isoformat()
        now_hm = datetime.now(CN_TZ).strftime("%H:%M")
        with open(target, "a", encoding="utf-8") as f:
            for rec in records:
                payload = {"date": today_str, "hm": now_hm, **rec, "sent": sent, "msg_id": msg_id}
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        return


def _compute_breadth_and_coverage(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]],
    has_baseline: bool,
) -> tuple[int, int, int, float, bool, str]:
    curr_a: dict[str, float] = {}
    red_count, green_count = 0, 0
    for sym, q in curr_quotes.items():
        if not is_a_share(sym):
            continue
        m = compute_quote_metrics(q)
        if m:
            curr_a[sym] = m["pct"]
            red_count += int(m["pct"] > 0)
            green_count += int(m["pct"] < 0)
    total_active = max(red_count + green_count, 1)
    red_ratio = red_count / total_active
    if not has_baseline:
        return red_count, green_count, 0, red_ratio, True, ""
    prev_a = {s: float(v["pct"]) for s, v in prev_q.items() if is_a_share(s) and isinstance(v, dict) and "pct" in v}
    common = set(curr_a).intersection(prev_a)
    prev_n, curr_n = len(prev_a), len(curr_a)
    if prev_n > 0 and len(common) < MIN_BREADTH_COVERAGE_RATIO * prev_n:
        gap_msg = (
            f"⚠️ **数据缺口：本帧只回来 {curr_n} 只 A 股（上一帧 {prev_n} 只，共同有效 {len(common)} 只 < 90%），"
            "已跳过广度与情绪骤变判定。**"
        )
        return red_count, green_count, 0, red_ratio, False, gap_msg
    d_red = sum(1 for s in common if curr_a[s] > 0) - sum(1 for s in common if prev_a[s] > 0)
    return red_count, green_count, d_red, red_ratio, True, ""


def _build_index_thermometer(curr_quotes: dict[str, dict[str, Any]], sh_pct: float, sz_pct: float) -> str:
    cy_m = compute_quote_metrics(curr_quotes.get("399006.SZ", {}))
    cy_part = f" 创业板 {cy_m['pct']:+.2f}%" if cy_m else ""
    a_pcts = [
        m["pct"] for sym, q in curr_quotes.items() if is_a_share(sym) and (m := compute_quote_metrics(q)) is not None
    ]
    med_part = f"（全A中位数 {statistics.median(a_pcts):+.2f}%）" if a_pcts else ""
    return f"上证 {sh_pct:+.2f}% 深成指 {sz_pct:+.2f}%{cy_part}{med_part}"


def _format_index_open_drift(curr_quotes: dict[str, dict[str, Any]]) -> str:
    sh = curr_quotes.get("000001.SH", {})
    if float(sh.get("open") or 0.0) <= 0:
        return ""
    sh_m = compute_quote_metrics(sh)
    if not sh_m:
        return ""
    sh_open = float(sh_m["open_pct"])
    sh_drift = float(sh_m["pct"]) - sh_open
    if sh_open >= 0.35 and sh_drift <= -0.25:
        return (
            f"🧭 **大盘高开验钞**：上证开盘 **{sh_open:+.2f}%** → 现 **{sh_m['pct']:+.2f}%**"
            f"（较开盘 **{sh_drift:+.2f}%** ⚠️ **高开回落防骗炮**：10:30前勿盲目追高开票，防守红利仓勿在分流低点误砍）"
        )
    if sh_open <= -0.20 and sh_drift >= 0.25:
        return (
            f"🧭 **大盘承接定性**：上证开盘 **{sh_open:+.2f}%** → 现 **{sh_m['pct']:+.2f}%**"
            f"（较开盘 **{sh_drift:+.2f}%** 🟢 **低开高走强承接**）"
        )
    return ""


def _enrich_mainline_and_telegraph(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]],
    has_baseline: bool,
    l2_map: dict[str, dict[str, Any]],
    cooldowns: dict[str, float],
    ts: float,
    buckets: dict[str, list[str]],
    alert_records: list[dict[str, Any]],
    fetch_telegraph: bool = False,
    turnover_dir: Path | None = None,
) -> None:
    try:
        from telegraph_catalysts import (
            scan_live_telegraph_catalysts,
            summarize_national_team_etfs,
            summarize_top_sector_mainlines,
            summarize_zero_bias_l2_leaders,
        )

        drift_note = _format_index_open_drift(curr_quotes)
        if drift_note:
            buckets["warnings"].append(drift_note)
        now_dt = datetime.fromtimestamp(ts, tz=CN_TZ)
        nt_summary, nt_l2_alert = summarize_national_team_etfs(
            curr_quotes, prev_q, has_baseline, now_dt=now_dt, turnover_dir=turnover_dir
        )
        if nt_l2_alert and _check_cooldown(cooldowns, "L2:nt_etf_surge", ts):
            buckets["l2_events"].insert(0, nt_l2_alert)
            alert_records.append({"ts": ts, "level": "L2", "kind": "national_team_etf_surge"})
        mainline_summary = summarize_top_sector_mainlines(curr_quotes, l2_map)
        if mainline_summary:
            buckets["mainlines"].append(mainline_summary)
        zero_bias_summary = summarize_zero_bias_l2_leaders(curr_quotes, l2_map)
        if zero_bias_summary:
            buckets["mainlines"].append(zero_bias_summary)
        if nt_summary:
            buckets["mainlines"].append(nt_summary)
        if fetch_telegraph:
            buckets["telegraph"].extend(
                scan_live_telegraph_catalysts(
                    l2_map, curr_quotes, window_minutes=30, max_items=4, pages=1, cooldowns=cooldowns, now_ts=ts
                )
            )
    except Exception as exc:
        print(f"[WARN] Mainline/telegraph enrichment failed: {exc}", file=sys.stderr)
        buckets["warnings"].append(f"⚠️ **主线/电报扩展模块异常：{exc}**")


def analyze_snapshot_diff(
    curr_quotes: dict[str, dict[str, Any]],
    prev_state: dict[str, Any],
    l2_map: dict[str, dict[str, Any]],
    holdings: dict[str, dict[str, float]],
    now_ts: float | None = None,
    turnover_dir: Path | None = None,
    alert_log_path: Path | None = None,
    fetch_telegraph: bool = False,
) -> tuple[str, str, str, int, dict[str, float]]:
    ts = now_ts if now_ts is not None else time.time()
    today_date = cn_today()
    today_str = today_date.isoformat()
    has_baseline = _is_fresh_baseline(prev_state, today_str, ts)
    prev_q = prev_state.get("quotes", {}) if has_baseline else {}
    cooldowns: dict[str, float] = dict(prev_state.get("cooldowns", {})) if prev_state.get("date") == today_str else {}

    sh = curr_quotes.get("000001.SH", {})
    sz = curr_quotes.get("399001.SZ", {})
    sh_m, sz_m = compute_quote_metrics(sh), compute_quote_metrics(sz)
    sh_amt = float(sh.get("amount") or 0.0) / 1e8
    sz_amt = float(sz.get("amount") or 0.0) / 1e8
    index_ok = sh_m is not None and sz_m is not None and sh_amt > 0 and sz_amt > 0
    tot_amt_yi = (sh_amt + sz_amt) if index_ok else 0.0
    sh_pct = sh_m["pct"] if sh_m else 0.0
    sz_pct = sz_m["pct"] if sz_m else 0.0
    index_gap_warn = (
        "⚠️ **指数数据缺口：缺少 000001.SH 或 399001.SZ 完整报价，两市成交额暂不合并计算。**"
        if not index_ok and ("000001.SH" in curr_quotes or "399001.SZ" in curr_quotes or len(curr_quotes) >= 50)
        else ""
    )

    red_count, green_count, d_red, red_ratio, coverage_ok, gap_msg = _compute_breadth_and_coverage(
        curr_quotes, prev_q, has_baseline
    )
    alert_records: list[dict[str, Any]] = []
    h_lines, h_l2, h_l3, h_l4 = _evaluate_holdings(
        curr_quotes, prev_q, has_baseline, holdings, cooldowns, ts, alert_records
    )
    buckets = _scan_equities_and_etfs(
        curr_quotes, prev_q, has_baseline, l2_map, red_ratio, coverage_ok, cooldowns, ts, alert_records
    )
    buckets["l2_events"] = h_l2 + buckets["l2_events"]
    buckets["l3"] = h_l3 + buckets["l3"]
    buckets["l4"] = h_l4 + buckets["l4"]
    missing_holdings = holdings_missing_quote_symbols(holdings, curr_quotes)
    if missing_holdings:
        joined = ", ".join(missing_holdings[:8])
        more = f" 等{len(missing_holdings)}只" if len(missing_holdings) > 8 else ""
        buckets["warnings"].append(
            f"⚠️ **持仓行情缺口：{joined}{more} 本帧无有效报价，止损/急跌/做T 监控已跳过这些标的**"
        )
        if _check_cooldown(cooldowns, "L2:holding_quote_gap", ts):
            buckets["l2_events"].insert(0, f"- 🟡 **【持仓行情缺口】** {joined}{more} 无报价，破位止损电话本帧无法判定")
            alert_records.append(
                {
                    "ts": ts,
                    "level": "L2",
                    "kind": "holding_quote_gap",
                    "symbols": missing_holdings,
                }
            )
    if gap_msg:
        buckets["warnings"].append(gap_msg)
    if index_gap_warn:
        buckets["warnings"].append(index_gap_warn)

    _enrich_mainline_and_telegraph(
        curr_quotes,
        prev_q,
        has_baseline,
        l2_map,
        cooldowns,
        ts,
        buckets,
        alert_records,
        fetch_telegraph=fetch_telegraph,
        turnover_dir=turnover_dir,
    )

    if coverage_ok and abs(d_red) >= 400 and _check_cooldown(cooldowns, "L2:breadth", ts):
        buckets["l2_events"].insert(
            0,
            f"- 🟡 **【市场情绪5m骤变】** 红盘家数 5 分钟变化 **{d_red:+d} 家**（当前红 {red_count} / 绿 {green_count}）",
        )
        alert_records.append({"ts": ts, "level": "L2", "kind": "breadth_shock", "d_red": d_red, "red": red_count})

    now_hm = datetime.fromtimestamp(ts, tz=CN_TZ).strftime("%H:%M")
    turnover_summary = update_and_compare_turnover(today_date, now_hm, tot_amt_yi, index_ok, turnover_dir=turnover_dir)
    _LAST_ALERT_RECORDS[:] = alert_records
    if alert_log_path is not None:
        append_alert_records(alert_records, log_path=alert_log_path, sent=True, msg_id="direct_call")
    index_thermo = _build_index_thermometer(curr_quotes, sh_pct, sz_pct)
    level = _determine_alert_level(buckets)
    title, body = _format_card(
        level,
        tot_amt_yi,
        index_ok,
        turnover_summary,
        index_thermo,
        red_count,
        green_count,
        d_red,
        h_lines,
        buckets,
        has_trace=bool(l2_map),
    )
    return level, title, body, red_count, cooldowns


def _determine_alert_level(buckets: dict[str, list[str]]) -> str:
    if buckets["l4"]:
        return "L4"
    if buckets["l3"]:
        return "L3"
    if buckets["l2_events"]:
        return "L2"
    return "L1"


def _build_urgent_focus_banner(
    level: str,
    now_hm: str,
    amt_tag: str,
    red_count: int,
    green_count: int,
    d_red: int,
    buckets: dict[str, list[str]],
) -> tuple[str, str]:
    default_title = f"【{level}】{now_hm} 5m全市场雷达：{amt_tag} | 红{red_count}/绿{green_count} ({d_red:+d})"
    if level not in ("L2", "L3", "L4"):
        return default_title, ""
    primary = buckets["l4"] if level == "L4" else (buckets["l3"] if level == "L3" else buckets["l2_events"])
    if not primary:
        return default_title, ""
    raw_first = primary[0].lstrip("- ").replace("*", "").replace("`", "").strip()
    short_first = raw_first.split("：")[0] if "：" in raw_first else raw_first[:38]
    title = f"【{level} 加急焦点】{now_hm} {short_first} | {amt_tag}"
    if "破位止损" in raw_first:
        hint = "持仓股触及止损线，请结合大盘跷跷板与分时均价线（VWAP）反抽力度决定去留。"
    elif "起跳板突破" in raw_first:
        hint = "满级起跳板标的放量突破且大盘强共振，可重点关注分时均线附近右侧机会。"
    elif "持仓急跌" in raw_first:
        hint = "持仓股 5 分钟急跌且跌破分时均线，请留意盘口承接与防守位。"
    elif "持仓做T高抛" in raw_first:
        hint = "持仓股拉升偏离分时均线 >3%，可关注冲高分批止盈或做 T 高抛机会。"
    elif level == "L3":
        hint = "威科夫低中位（RPS<=80）地量/吸筹稳健标的 5 分钟放量点火且站稳均线，可重点关注。"
    else:
        hint = "主线板块或宽基 ETF 出现 5 分钟集中放量异动，请留意资金进攻方向。"
    items = [p if p.startswith("-") else f"- {p}" for p in primary[:3]]
    banner = (
        f"🚨 **【本次 {level} 加急核心焦点 — 为什么给你发加急】**\n"
        + "\n".join(items)
        + f"\n💡 **关注重点**：{hint}\n---"
    )
    return title, banner


def _format_card(
    level: str,
    tot_amt_yi: float,
    index_ok: bool,
    turnover_summary: str,
    index_thermo: str,
    red_count: int,
    green_count: int,
    d_red: int,
    h_lines: list[str],
    buckets: dict[str, list[str]],
    has_trace: bool = True,
) -> tuple[str, str]:
    now_hm = datetime.now(CN_TZ).strftime("%H:%M")
    amt_tag = f"两市{tot_amt_yi:.0f}亿" if index_ok else "两市成交缺口"
    title, focus_banner = _build_urgent_focus_banner(level, now_hm, amt_tag, red_count, green_count, d_red, buckets)
    secs: list[str] = []
    if focus_banner:
        secs.append(focus_banner)
    if not has_trace:
        secs.append(
            "⚠️ **今日无威科夫 trace，L3/L4/板块共振已停用（请检查 ~/.wyckoff/traces 或同步 Wyckoff Funnel 产物）**"
        )
    for w in buckets.get("warnings", []):
        secs.append(w)
    secs.append(
        f"全市场 7,807 标的 5 分钟扫描完成：{turnover_summary} | {index_thermo} | "
        f"上涨 {red_count} 家 / 下跌 {green_count} 家（同池5m变化 {d_red:+d} 家）。"
    )
    if h_lines:
        secs.append("**1. 持仓5分钟贴身监控**\n" + "\n".join(h_lines))
    if buckets["l4"]:
        secs.append("**2. 🔴 L4 电话级警报（持仓破止损 / 满级起跳板突破）**\n" + "\n".join(buckets["l4"][:5]))
    if buckets["l3"]:
        secs.append("**3. 🟠 L3 短信级：威科夫 L2+L3 地量/吸筹 5m 点火 & 持仓急跌**\n" + "\n".join(buckets["l3"][:8]))
    if buckets["l1_obs"]:
        secs.append("**3b. 👀 早盘 L2+L3 异动观察（10:30前验钞期·不发短信加急）**\n" + "\n".join(buckets["l1_obs"][:6]))
    if buckets["l2_events"]:
        secs.append("**4. 🟡 L2 加急事件（持仓做T / 板块共振 / 情绪骤变）**\n" + "\n".join(buckets["l2_events"][:6]))
    if buckets["etf_up"] or buckets["etf_down"]:
        secs.append("**5. 全市场 ETF 5m 异动榜**\n" + "\n".join(buckets["etf_up"][:6] + buckets["etf_down"][:5]))
    if buckets["stock_up"] or buckets["stock_down"]:
        secs.append("**6. 全市场 A 股 5m 异动榜**\n" + "\n".join(buckets["stock_up"][:6] + buckets["stock_down"][:6]))
    for m_block in buckets.get("mainlines", []):
        secs.append(m_block)
    if buckets.get("telegraph"):
        secs.append("**7. ⚡ 7x24 实时电报催化与威科夫映射**\n" + "\n".join(buckets["telegraph"][:5]))
    secs.append("⚠️ 风险提醒: 严守分时均价线（VWAP）与量比确认，不在均线下方接飞刀。")
    return title, "\n\n".join(secs)


def should_send_card(
    level: str,
    now_ts: float,
    last_l1_ts: float,
    force_send: bool,
    urgent_only: bool,
) -> bool:
    if urgent_only:
        return level in ("L2", "L3", "L4")
    if force_send or level in ("L2", "L3", "L4"):
        return True
    return (now_ts - last_l1_ts) >= L1_INTERVAL_SECONDS


def send_feishu_tiered_alert(
    title: str,
    content: str,
    level: str = "L1",
    user_open_id: str = "",
    dry_run: bool = False,
) -> str | None:
    if dry_run:
        print(f"[DRY-RUN {level}] {title}\n{content}")
        return "dry_run_msg_id"
    if not user_open_id:
        print(f"[SKIP-FEISHU {level}] No feishu_user_open_id configured. {title}", file=sys.stderr)
        return None
    card_payload = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": report_card_template(title, content),
        },
        "elements": build_report_card_elements(content),
    }
    send_cmd = [
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
        json.dumps(card_payload, ensure_ascii=False),
    ]
    res = subprocess.run(send_cmd, capture_output=True, text=True, check=False)
    msg_id = None
    if res.returncode == 0 and res.stdout.strip():
        try:
            resp_json = json.loads(res.stdout)
            msg_id = (resp_json.get("data") or {}).get("message_id") or resp_json.get("message_id")
        except Exception:
            msg_id = None
    if not msg_id:
        print(
            f"[ERROR-FEISHU {level}] Failed to send card: rc={res.returncode} stderr={res.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    urgent_map = {"L2": "urgent_app", "L3": "urgent_sms", "L4": "urgent_phone"}
    if level in urgent_map:
        urg_res = subprocess.run(
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
        if urg_res.returncode != 0:
            print(f"[ERROR-FEISHU {level}] Urgent buzz failed: {urg_res.stderr.strip()}", file=sys.stderr)
            # Incomplete L2/L3/L4 delivery: caller must not persist cooldown (esp. 86400s stop phone).
            return None
    return msg_id


def _backfill_holding_quotes(
    client: Any,
    curr_quotes: dict[str, dict[str, Any]],
    holdings: dict[str, dict[str, float]],
) -> dict[str, dict[str, Any]]:
    """Universe snapshots can omit symbols; re-fetch holdings so stop-loss is not silently skipped."""
    missing = holdings_missing_quote_symbols(holdings, curr_quotes)
    if not missing:
        return curr_quotes
    try:
        extra = client.get_quotes(symbols=missing)
    except Exception as exc:
        print(f"[WARN] Holding quote backfill failed for {missing}: {exc}", file=sys.stderr)
        return curr_quotes
    if not extra:
        return curr_quotes
    merged = dict(curr_quotes)
    merged.update(extra)
    return merged


def _format_heartbeat_card(
    curr_quotes: dict[str, dict[str, Any]],
    holdings: dict[str, dict[str, float]],
    red_count: int,
    now_hm: str,
) -> tuple[str, str]:
    sh_m = compute_quote_metrics(curr_quotes.get("000001.SH", {}))
    sz_m = compute_quote_metrics(curr_quotes.get("399001.SZ", {}))
    cy_m = compute_quote_metrics(curr_quotes.get("399006.SZ", {}))
    sh_s = f"上证{sh_m['pct']:+.2f}%" if sh_m else "上证--"
    cy_s = f" 创{cy_m['pct']:+.2f}%" if cy_m else ""
    thermo = (
        _build_index_thermometer(curr_quotes, sh_m["pct"], sz_m["pct"])
        if sh_m and sz_m
        else f"{sh_s}{' 深' + format(sz_m['pct'], '+.2f') + '%' if sz_m else ' 深--'}{cy_s}"
    )
    title = f"【💓 心跳】{now_hm} 雷达在线 | {sh_s}{cy_s} | 红{red_count}家"
    lines = [f"✅ 7,807 标的 5m 巡检正常（本帧无 L2/L3/L4 加急异动）：{thermo} | 红盘 {red_count} 家"]
    if holdings:
        h_parts, any_stop = [], False
        for sym, cfg in holdings.items():
            hm = compute_quote_metrics(curr_quotes.get(sym, {}))
            breached = bool(hm and cfg.get("stop", 0.0) > 0 and hm["last"] <= cfg["stop"])
            any_stop = any_stop or breached
            tag = "🔴破止损" if breached else ""
            h_parts.append(f"{sym[:6]} {hm['pct']:+.2f}%{tag}" if hm else f"{sym[:6]} --")
        prefix = "⚠️ 持仓破位待处理：" if any_stop else "🛡️ 持仓正常："
        lines.append(prefix + " | ".join(h_parts))
    return title, "\n".join(lines)


def _baseline_after_send(
    prev_state: dict[str, Any],
    today_str: str,
    now_ts: float,
    compact_snaps: dict[str, dict[str, float]],
    send_attempted: bool,
    send_ok: bool,
) -> tuple[float, dict[str, dict[str, float]]]:
    """Keep prior quote baseline when a card send was attempted but failed."""
    if send_attempted and not send_ok and prev_state.get("date") == today_str and prev_state.get("quotes"):
        return float(prev_state.get("ts") or now_ts), dict(prev_state["quotes"])
    return now_ts, compact_snaps


def run_once(args: argparse.Namespace) -> None:
    now_bjt = datetime.now(CN_TZ)
    if not args.ignore_hours and not args.dry_run and not is_cn_trading_session(now_bjt):
        print(f"[{now_bjt.strftime('%Y-%m-%d %H:%M:%S')}] Outside A-share trading hours, skipping.")
        return
    load_env_file()
    from integrations.tickflow_client import TickFlowClient

    client = TickFlowClient(api_key=os.environ.get("TICKFLOW_API_KEY", ""))
    l2_map = load_wyckoff_l2_map(args.trace)
    holdings = parse_holdings_arg(args.holding)
    prev_state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    now_ts = time.time()
    curr_quotes = _backfill_holding_quotes(
        client, client.get_quotes(universes=["CN_Equity_A", "CN_ETF", "CN_Index"]), holdings
    )
    level, title, content, red_count, cooldowns = analyze_snapshot_diff(
        curr_quotes, prev_state, l2_map, holdings, now_ts=now_ts, fetch_telegraph=not args.dry_run
    )
    compact_snaps = {
        sym: {"pct": round(m["pct"], 3), "amt_yi": round(m["amt_yi"], 3)}
        for sym, q in curr_quotes.items()
        if (m := compute_quote_metrics(q)) is not None
    }
    today_str = cn_today().isoformat()
    prev_cooldowns: dict[str, float] = (
        dict(prev_state.get("cooldowns", {})) if prev_state.get("date") == today_str else {}
    )
    last_l1_ts = float(prev_state.get("last_l1_ts") or 0.0) if prev_state.get("date") == today_str else 0.0
    saved_cooldowns = prev_cooldowns
    send_attempted, send_ok = False, False
    if should_send_card(level, now_ts, last_l1_ts, args.force_send, args.urgent_only):
        send_attempted = True
        open_id = "" if args.dry_run else resolve_lark_open_id(args.user_id)
        msg_id = send_feishu_tiered_alert(title, content, level=level, user_open_id=open_id, dry_run=args.dry_run)
        if msg_id:
            send_ok, saved_cooldowns = True, cooldowns
            if not args.dry_run:
                append_alert_records(_LAST_ALERT_RECORDS, log_path=ALERT_LOG_PATH, sent=True, msg_id=msg_id)
            if level == "L1":
                last_l1_ts = now_ts
            print(f"[{now_bjt.strftime('%H:%M:%S')}] Sent {level} alert: msg_id={msg_id}")
    elif getattr(args, "heartbeat", False) and not args.urgent_only:
        hb_title, hb_body = _format_heartbeat_card(curr_quotes, holdings, red_count, now_bjt.strftime("%H:%M"))
        open_id = "" if args.dry_run else resolve_lark_open_id(args.user_id)
        msg_id = send_feishu_tiered_alert(hb_title, hb_body, level="L1", user_open_id=open_id, dry_run=args.dry_run)
        if msg_id:
            print(f"[{now_bjt.strftime('%H:%M:%S')}] Sent HEARTBEAT card: msg_id={msg_id}")
    if args.dry_run:
        return
    state_ts, saved_quotes = _baseline_after_send(prev_state, today_str, now_ts, compact_snaps, send_attempted, send_ok)
    STATE_PATH.write_text(
        json.dumps(
            {
                "date": today_str,
                "ts": state_ts,
                "last_l1_ts": last_l1_ts,
                "red": red_count,
                "cooldowns": saved_cooldowns,
                "quotes": saved_quotes,
            }
        ),
        encoding="utf-8",
    )


def _run_daemon_loop(args: argparse.Namespace) -> None:
    interval = max(args.interval, 30)
    err_count = 0
    while True:
        try:
            run_once(args)
            err_count = 0
        except Exception as exc:
            err_count += 1
            print(
                f"[{datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')}] ERROR in radar loop ({err_count}): {exc}",
                file=sys.stderr,
            )
            if err_count == 3:
                open_id = "" if args.dry_run else resolve_lark_open_id(args.user_id)
                send_feishu_tiered_alert(
                    "【L1 雷达故障告警】盘中雷达连续 3 次扫描异常",
                    f"盘中雷达连续 3 次执行异常，请检查网络或行情源。\n最近错误：`{exc}`",
                    level="L1",
                    user_open_id=open_id,
                    dry_run=args.dry_run,
                )
        sleep_s = max(5.0, interval - (time.time() % interval))
        time.sleep(sleep_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="5-minute full-market intraday radar (09:30-15:00 BJT)")
    parser.add_argument("--once", action="store_true", help="Run a single 5-min scan")
    parser.add_argument("--daemon", action="store_true", help="Run every 5 minutes during trading hours")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval seconds (default 300 = 5m)")
    parser.add_argument("--trace", type=str, default=None, help="Path to review_trace_*.json.gz")
    parser.add_argument("--holding", action="append", default=[], help="Holding spec CODE:COST:STOP (repeatable)")
    parser.add_argument(
        "--user-id", type=str, default=None, help="Override Feishu open_id (default: from lark-cli auth status)"
    )
    parser.add_argument("--urgent-only", action="store_true", help="Only send Feishu card when L2/L3/L4 triggers")
    parser.add_argument("--force-send", action="store_true", help="Always send full Feishu card on every scan")
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="Send 2-line alive heartbeat card on routine 5m scans while keeping full L1 card every ~30m",
    )
    parser.add_argument("--ignore-hours", action="store_true", help="Run scan even outside trading hours")
    parser.add_argument("--dry-run", action="store_true", help="Print card without sending to Feishu")
    args = parser.parse_args()
    if args.daemon:
        _run_daemon_loop(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
