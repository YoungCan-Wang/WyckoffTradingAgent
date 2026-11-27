#!/usr/bin/env python3
"""TickFlow WebSocket real-time streaming daemon (200 priority symbols, sub-second L4/L2 + shared cache)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[3]
for _p in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intraday_radar import (  # noqa: E402
    ALERT_LOG_PATH,
    HOLDING_STOP_COOLDOWN_SECONDS,
    STATE_PATH,
    _check_cooldown,
    _is_t1_locked,
    _normalize_holding_symbol,
    append_alert_records,
    compute_quote_metrics,
    is_cn_trading_session,
    load_env_file,
    load_wyckoff_l2_map,
    parse_holdings_arg,
    resolve_lark_open_id,
    send_feishu_tiered_alert,
)
from telegraph_catalysts import NATIONAL_TEAM_20_ETFS, THEME_ETF_UNIVERSE  # noqa: E402

from utils.trading_clock import CN_TZ, cn_today  # noqa: E402

WS_STREAM_URL = "wss://api.tickflow.org/v1/ws/stream"
MAX_WS_SYMBOLS = 200
WS_CACHE_PATH = Path("/tmp/wyckoff_tickflow_ws_cache.json")
WS_STATE_PATH = Path("/tmp/wyckoff_tickflow_ws_state.json")
CACHE_FLUSH_INTERVAL_S = 2.0
EVAL_INTERVAL_S = 3.0
ROLLING_WINDOW_S = 60.0
RING_PRUNE_AGE_S = 75.0
WS_MIN_CARD_GAP_S = 90.0
MIN_RING_AGE_FOR_DELTA_S = 5.0
CORE_INDICES = (
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000688.SH",
    "000300.SH",
    "000016.SH",
    "000905.SH",
    "000852.SH",
)
BELLWETHER_EQUITIES = (
    "300308.SZ",
    "300502.SZ",
    "688256.SH",
    "688981.SH",
    "002371.SZ",
    "300750.SZ",
    "300059.SZ",
    "601398.SH",
    "601857.SH",
    "601088.SH",
)


def _code6_to_symbol(code6: str) -> str:
    """Resolve 6-digit A-share code to TickFlow symbol (.SH/.SZ/.BJ)."""
    return _normalize_holding_symbol(code6)


def build_ws_priority_symbols(
    holdings: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    watch_symbols: list[str] | None = None,
    max_symbols: int = MAX_WS_SYMBOLS,
) -> list[str]:
    """Build ordered unique list of up to max_symbols (200) priority WS symbols."""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(sym: str) -> None:
        norm = _normalize_holding_symbol(sym)
        if norm and norm not in seen and len(ordered) < max_symbols:
            seen.add(norm)
            ordered.append(norm)

    for sym in holdings:
        _add(sym)
    for sym in watch_symbols or []:
        _add(sym)
    for sym in (*CORE_INDICES, "511360.SH"):
        _add(sym)
    for sym in NATIONAL_TEAM_20_ETFS:
        _add(sym)
    for _, sym, _ in THEME_ETF_UNIVERSE:
        _add(sym)
    for sym in BELLWETHER_EQUITIES:
        _add(sym)

    ranked_pool = [
        (
            1 if meta.get("funnel_tag") == "🏆3日候选连入" else 0,
            1 if meta.get("stage") == "候选池已捕获" else 0,
            1 if meta.get("shadow_lane") == "pre_breakout" else 0,
            int(meta.get("streak_days") or 0),
            float(meta.get("rps") or 0.0),
            code,
        )
        for code, meta in (l2_map or {}).items()
        if str(meta.get("stage") or "") != "风控拦截"
    ]
    ranked_pool.sort(reverse=True)
    for _, _, _, _, _, code in ranked_pool:
        if len(ordered) >= max_symbols:
            break
        _add(_code6_to_symbol(code))
    return ordered[:max_symbols]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist JSON state/cache via temp file replace."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _update_ring_60s(
    ring: dict[str, deque[tuple[float, float, float]]],
    quote: dict[str, Any],
    now_ts: float,
) -> None:
    """Append tick sample into rolling 60s buffer and prune stale entries."""
    sym = str(quote.get("symbol") or "").strip().upper()
    m = compute_quote_metrics(quote)
    if not sym or m is None:
        return
    dq = ring.setdefault(sym, deque())
    dq.append((now_ts, float(m["pct"]), float(m["amt_yi"])))
    cutoff = now_ts - RING_PRUNE_AGE_S
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def _oldest_within_window(
    dq: deque[tuple[float, float, float]] | None,
    now_ts: float,
    window_s: float = ROLLING_WINDOW_S,
) -> tuple[float, float, float] | None:
    """Return earliest sample within rolling window if old enough for real delta."""
    if not dq:
        return None
    cutoff = now_ts - window_s
    anchor = next((pt for pt in dq if pt[0] >= cutoff), dq[-1])
    if now_ts - anchor[0] < MIN_RING_AGE_FOR_DELTA_S:
        return None
    return anchor


def load_shared_cooldowns(
    today_str: str,
    ws_cooldowns: dict[str, float],
    state_path: Path = STATE_PATH,
) -> dict[str, float]:
    """Merge today's 5m radar cooldowns with WS cooldowns to avoid duplicate L4/L2 alerts."""
    merged = dict(ws_cooldowns)
    if state_path.exists():
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
            if st.get("date") == today_str:
                for k, v in (st.get("cooldowns") or {}).items():
                    merged[k] = max(float(merged.get(k) or 0.0), float(v or 0.0))
        except Exception:
            pass
    return merged


def _eval_holdings_realtime(
    live_quotes: dict[str, dict[str, Any]],
    ring_60s: dict[str, deque[tuple[float, float, float]]],
    holdings: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    cooldowns: dict[str, float],
    now_ts: float,
) -> tuple[list[str], list[str], list[dict[str, Any]], list[str]]:
    """Evaluate sub-second L4 hard stop and L2 60s surge/drop on holdings."""
    l4_lines: list[str] = []
    l2_lines: list[str] = []
    records: list[dict[str, Any]] = []
    focus: list[str] = []
    for sym, cfg in holdings.items():
        if _is_t1_locked(cfg, now_ts):
            continue
        q = live_quotes.get(sym)
        m = compute_quote_metrics(q) if q else None
        if not m or m["last"] <= 0:
            continue
        nm = (q.get("ext") or {}).get("name") or (l2_map.get(sym[:6]) or {}).get("name") or sym
        stop = float(cfg.get("stop") or 0.0)
        if stop > 0 and m["last"] <= stop:
            if _check_cooldown(cooldowns, f"L4:holding_stop:{sym}", now_ts, window=HOLDING_STOP_COOLDOWN_SECONDS):
                l4_lines.append(
                    f"- 🔴 **【WS秒级破止损】`{sym}` {nm}**：现价 **{m['last']:.3f} ({m['pct']:+.2f}%)** <= 硬止损 **{stop:.3f}**"
                )
                records.append(
                    {
                        "ts": now_ts,
                        "level": "L4",
                        "kind": "holding_stop",
                        "symbol": sym,
                        "last": m["last"],
                        "stop": stop,
                    }
                )
                focus.append(f"持仓破止损 {sym}({m['last']:.2f})")
            continue
        anchor = _oldest_within_window(ring_60s.get(sym), now_ts)
        if anchor is None:
            continue
        d_pct = m["pct"] - anchor[1]
        if d_pct >= 3.2 and _check_cooldown(cooldowns, f"L2:ws_hold_surge:{sym}", now_ts):
            l2_lines.append(
                f"- 🟡 **【WS 60s持仓急拉做T】`{sym}` {nm}**：60s涨速 **{d_pct:+.2f}%** → 现 **{m['pct']:+.2f}%** (VWAP {m['vwap_dev']:+.2f}%)"
            )
            records.append(
                {"ts": now_ts, "level": "L2", "kind": "ws_holding_surge", "symbol": sym, "d_pct_60s": round(d_pct, 2)}
            )
            focus.append(f"持仓急拉 {sym}({d_pct:+.1f}%)")
        elif d_pct <= -2.8 and m["vwap_dev"] <= -0.6 and _check_cooldown(cooldowns, f"L2:ws_hold_drop:{sym}", now_ts):
            l2_lines.append(
                f"- 🟠 **【WS 60s持仓跳水预警】`{sym}` {nm}**：60s跌速 **{d_pct:+.2f}%** | VWAP水下 **{m['vwap_dev']:+.2f}%**"
            )
            records.append(
                {"ts": now_ts, "level": "L2", "kind": "ws_holding_drop", "symbol": sym, "d_pct_60s": round(d_pct, 2)}
            )
            focus.append(f"持仓跳水 {sym}({d_pct:+.1f}%)")
    return l4_lines, l2_lines, records, focus


def _eval_nt_etf_blitz(
    live_quotes: dict[str, dict[str, Any]],
    ring_60s: dict[str, deque[tuple[float, float, float]]],
    cooldowns: dict[str, float],
    now_ts: float,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Detect 60-second institutional broad-ETF blitz on National Team 20 ETFs."""
    deltas: list[tuple[float, str, str, float]] = []
    tot_d = 0.0
    for sym, label in NATIONAL_TEAM_20_ETFS.items():
        q = live_quotes.get(sym)
        m = compute_quote_metrics(q) if q else None
        anchor = _oldest_within_window(ring_60s.get(sym), now_ts)
        if not m or anchor is None:
            continue
        d_amt = max(0.0, m["amt_yi"] - anchor[2])
        if d_amt > 0:
            tot_d += d_amt
            deltas.append((d_amt, sym[:6], label, m["pct"]))
    deltas.sort(reverse=True)
    max_single = deltas[0][0] if deltas else 0.0
    if (max_single >= 3.5 or tot_d >= 8.0) and _check_cooldown(cooldowns, "L2:ws_nt_blitz", now_ts):
        top_txt = "、".join(f"`{c}`({lbl} +{d:.1f}亿/{p:+.2f}%)" for d, c, lbl, p in deltas[:3])
        line = f"- 🚀 **【WS 60s国家队ETF爆量】** 20大ETF 60s增量 **+{tot_d:.1f}亿**（单只峰值 +{max_single:.1f}亿）：{top_txt}"
        rec = {"ts": now_ts, "level": "L2", "kind": "ws_nt_blitz", "tot_d_60s": round(tot_d, 2)}
        return [line], [rec], [f"国家队60s爆量 +{tot_d:.1f}亿"]
    return [], [], []


def _eval_core_breakouts(
    live_quotes: dict[str, dict[str, Any]],
    ring_60s: dict[str, deque[tuple[float, float, float]]],
    holdings: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    watch_set: set[str],
    cooldowns: dict[str, float],
    now_ts: float,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Detect 60s low-wick above-VWAP breakouts inside core funnel / watch stocks."""
    lines: list[str] = []
    recs: list[dict[str, Any]] = []
    focus: list[str] = []
    for sym, q in live_quotes.items():
        if sym in holdings:
            continue
        meta = l2_map.get(sym[:6]) or {}
        if not meta or meta.get("stage") == "风控拦截":
            continue
        in_core = (
            sym in watch_set
            or meta.get("funnel_tag") == "🏆3日候选连入"
            or meta.get("stage") == "候选池已捕获"
            or meta.get("shadow_lane") == "pre_breakout"
        )
        if not in_core:
            continue
        m = compute_quote_metrics(q)
        anchor = _oldest_within_window(ring_60s.get(sym), now_ts)
        if not m or anchor is None:
            continue
        d_pct = m["pct"] - anchor[1]
        if not (
            d_pct >= 2.5
            and m["open_pct"] <= 2.5
            and 0.0 <= m["vwap_dev"] <= 2.2
            and m["upper_wick_pct"] <= 1.0
            and m["amt_yi"] >= 1.5
        ):
            continue
        if _check_cooldown(cooldowns, f"L2:ws_breakout:{sym}", now_ts, window=3600):
            nm = meta.get("name") or (q.get("ext") or {}).get("name") or sym
            sec = meta.get("sector") or ""
            lines.append(
                f"- 🎯 **【WS 60s核心池点火】`{sym}` {nm}** [{sec}]：60s急拉 **{d_pct:+.2f}%** → 现 **{m['pct']:+.2f}%** | VWAP {m['vwap_dev']:+.2f}% | 额 {m['amt_yi']:.1f}亿"
            )
            recs.append(
                {"ts": now_ts, "level": "L2", "kind": "ws_core_breakout", "symbol": sym, "d_pct_60s": round(d_pct, 2)}
            )
            focus.append(f"核心点火 {nm}({m['pct']:+.1f}%)")
    return lines, recs, focus


def evaluate_ws_realtime_triggers(
    live_quotes: dict[str, dict[str, Any]],
    ring_60s: dict[str, deque[tuple[float, float, float]]],
    holdings: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    cooldowns: dict[str, float],
    now_ts: float,
    last_l2_card_ts: float,
    watch_symbols: list[str] | None = None,
    state_path: Path = STATE_PATH,
) -> tuple[str | None, str, str, list[dict[str, Any]], dict[str, float], float]:
    """Run all sub-second WS rules with shared cooldown dedup and anti-flood gating."""
    today_str = datetime.fromtimestamp(now_ts, tz=CN_TZ).date().isoformat()
    merged = load_shared_cooldowns(today_str, cooldowns, state_path=state_path)
    watch_set = {_normalize_holding_symbol(s) for s in (watch_symbols or [])}

    l4_lines, h_l2, h_recs, h_focus = _eval_holdings_realtime(live_quotes, ring_60s, holdings, l2_map, merged, now_ts)
    nt_lines, nt_recs, nt_focus = _eval_nt_etf_blitz(live_quotes, ring_60s, merged, now_ts)
    bk_lines, bk_recs, bk_focus = _eval_core_breakouts(
        live_quotes, ring_60s, holdings, l2_map, watch_set, merged, now_ts
    )

    all_recs = [*h_recs, *nt_recs, *bk_recs]
    if not all_recs:
        return None, "", "", [], cooldowns, last_l2_card_ts
    has_l4 = bool(l4_lines)
    if not has_l4 and (now_ts - last_l2_card_ts) < WS_MIN_CARD_GAP_S:
        return None, "", "", [], cooldowns, last_l2_card_ts

    level = "L4" if has_l4 else "L2"
    focus_all = [*h_focus, *nt_focus, *bk_focus]
    hm = datetime.fromtimestamp(now_ts, tz=CN_TZ).strftime("%H:%M:%S")
    title = f"【{level} WebSocket秒级焦点】{hm} {focus_all[0]}"
    sections = [
        f"🚨 **【WebSocket 秒级实时引擎触发 · {hm}】**（订阅 {len(live_quotes)}/{MAX_WS_SYMBOLS} 只核心标的）",
        *l4_lines,
        *h_l2,
        *nt_lines,
        *bk_lines,
    ]
    new_l2_ts = last_l2_card_ts if has_l4 else now_ts
    return level, title, "\n".join(sections), all_recs, merged, new_l2_ts


def is_fatal_ws_error(exc_or_msg: Exception | str) -> bool:
    """Stop reconnecting on HTTP 401/403 or explicit WS permission errors per TickFlow spec."""
    text = str(exc_or_msg or "").lower()
    if any(
        tok in text
        for tok in ("http 401", "http 403", "status code 401", "status code 403", "no permission", "unauthorized")
    ):
        return True
    resp = getattr(exc_or_msg, "response", None)
    return getattr(resp, "status_code", None) in (401, 403)


def _flush_and_eval_if_due(
    live_quotes: dict[str, dict[str, Any]],
    ring_60s: dict[str, deque[tuple[float, float, float]]],
    holdings: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    cooldowns: dict[str, float],
    watch_symbols: list[str],
    now_ts: float,
    last_flush_ts: float,
    last_eval_ts: float,
    last_l2_card_ts: float,
    args: argparse.Namespace,
) -> tuple[float, float, dict[str, float], float]:
    """Persist shared WS quote cache and evaluate sub-second alerts when intervals elapse."""
    if live_quotes and (now_ts - last_flush_ts) >= CACHE_FLUSH_INTERVAL_S:
        atomic_write_json(
            WS_CACHE_PATH,
            {"date": cn_today().isoformat(), "updated_ts": now_ts, "count": len(live_quotes), "quotes": live_quotes},
        )
        last_flush_ts = now_ts
    if live_quotes and (now_ts - last_eval_ts) >= EVAL_INTERVAL_S:
        last_eval_ts = now_ts
        level, title, body, recs, new_cds, new_l2_ts = evaluate_ws_realtime_triggers(
            live_quotes, ring_60s, holdings, l2_map, cooldowns, now_ts, last_l2_card_ts, watch_symbols=watch_symbols
        )
        if level:
            open_id = "" if args.dry_run else resolve_lark_open_id(args.user_id)
            msg_id = send_feishu_tiered_alert(title, body, level=level, user_open_id=open_id, dry_run=args.dry_run)
            if msg_id:
                cooldowns, last_l2_card_ts = new_cds, new_l2_ts
                if not args.dry_run:
                    append_alert_records(recs, log_path=ALERT_LOG_PATH, sent=True, msg_id=msg_id)
                atomic_write_json(
                    WS_STATE_PATH,
                    {"date": cn_today().isoformat(), "updated_ts": now_ts, "cooldowns": cooldowns},
                )
                print(
                    f"[{datetime.fromtimestamp(now_ts, tz=CN_TZ).strftime('%H:%M:%S')}] Sent WS {level} alert: msg_id={msg_id}",
                    flush=True,
                )
    return last_flush_ts, last_eval_ts, cooldowns, last_l2_card_ts


async def _ws_session_once(
    url: str,
    priority_syms: list[str],
    holdings: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    cooldowns: dict[str, float],
    watch_symbols: list[str],
    deadline: float | None,
    args: argparse.Namespace,
) -> tuple[dict[str, float], bool]:
    """Run one TickFlow WebSocket session until disconnect, error, session end, or deadline."""
    import websockets

    live_quotes: dict[str, dict[str, Any]] = {}
    ring_60s: dict[str, deque[tuple[float, float, float]]] = {}
    last_flush_ts = 0.0
    last_eval_ts = 0.0
    last_l2_card_ts = 0.0
    async with websockets.connect(
        url, open_timeout=8, ping_interval=25, ping_timeout=20, max_size=16 * 1024 * 1024
    ) as ws:
        await ws.send(json.dumps({"op": "subscribe", "channel": "quotes", "symbols": priority_syms}))
        print(
            f"[{datetime.now(CN_TZ).strftime('%H:%M:%S')}] WS connected, subscribing {len(priority_syms)} symbols...",
            flush=True,
        )
        while True:
            now_ts = time.time()
            if deadline is not None and now_ts >= deadline:
                return cooldowns, True
            if not args.ignore_hours and not is_cn_trading_session(datetime.fromtimestamp(now_ts, tz=CN_TZ)):
                return cooldowns, False
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(raw)
                op = msg.get("op")
                if op == "quotes":
                    for q in msg.get("data") or []:
                        sym = str(q.get("symbol") or "").strip().upper()
                        if sym:
                            live_quotes[sym] = q
                            _update_ring_60s(ring_60s, q, now_ts)
                elif op == "subscribed":
                    print(
                        f"[{datetime.now(CN_TZ).strftime('%H:%M:%S')}] WS subscribed total={msg.get('total')}",
                        flush=True,
                    )
                elif op == "error":
                    err_msg = str(msg.get("message") or "")
                    print(f"[WS-ERROR] {err_msg}", file=sys.stderr, flush=True)
                    if is_fatal_ws_error(err_msg):
                        raise RuntimeError(f"fatal ws error: {err_msg}")
            except TimeoutError:
                pass
            now_ts = time.time()
            last_flush_ts, last_eval_ts, cooldowns, last_l2_card_ts = _flush_and_eval_if_due(
                live_quotes,
                ring_60s,
                holdings,
                l2_map,
                cooldowns,
                watch_symbols,
                now_ts,
                last_flush_ts,
                last_eval_ts,
                last_l2_card_ts,
                args,
            )


async def run_ws_stream(args: argparse.Namespace) -> None:
    """Persistent WebSocket daemon with trading-session sleep and reconnect recovery."""
    load_env_file()
    api_key = os.environ.get("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] Missing TICKFLOW_API_KEY", file=sys.stderr, flush=True)
        return
    holdings = parse_holdings_arg(args.holding)
    l2_map = load_wyckoff_l2_map(args.trace)
    watch_symbols = [str(w).strip() for w in (args.watch or []) if str(w).strip()]
    priority_syms = build_ws_priority_symbols(holdings, l2_map, watch_symbols=watch_symbols)
    url = f"{WS_STREAM_URL}?api_key={api_key}"
    today_str = cn_today().isoformat()
    cooldowns: dict[str, float] = {}
    if WS_STATE_PATH.exists():
        try:
            st = json.loads(WS_STATE_PATH.read_text(encoding="utf-8"))
            if st.get("date") == today_str:
                cooldowns = {k: float(v) for k, v in (st.get("cooldowns") or {}).items()}
        except Exception:
            pass
    deadline = (time.time() + float(args.max_seconds)) if args.max_seconds else None
    while True:
        now_ts = time.time()
        if deadline is not None and now_ts >= deadline:
            break
        if cn_today().isoformat() != today_str:
            today_str = cn_today().isoformat()
            cooldowns = {}
            l2_map = load_wyckoff_l2_map(args.trace)
            priority_syms = build_ws_priority_symbols(holdings, l2_map, watch_symbols=watch_symbols)
        if not args.ignore_hours and not is_cn_trading_session(datetime.fromtimestamp(now_ts, tz=CN_TZ)):
            await asyncio.sleep(15.0)
            continue
        try:
            cooldowns, hit_deadline = await _ws_session_once(
                url, priority_syms, holdings, l2_map, cooldowns, watch_symbols, deadline, args
            )
            if hit_deadline:
                break
        except Exception as exc:
            print(f"[{datetime.now(CN_TZ).strftime('%H:%M:%S')}] WS disconnected: {exc}", file=sys.stderr, flush=True)
            if is_fatal_ws_error(exc):
                break
            await asyncio.sleep(3.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="TickFlow WebSocket 200-symbol real-time stream daemon")
    parser.add_argument(
        "--holding", action="append", default=[], help="Holding spec CODE:COST:STOP[:ATK|DEF][:YYYYMMDD]"
    )
    parser.add_argument("--watch", action="append", default=[], help="Extra watch symbol CODE (repeatable)")
    parser.add_argument("--trace", type=str, default=None, help="Path to review_trace_*.json.gz")
    parser.add_argument("--user-id", type=str, default=None, help="Override Feishu open_id")
    parser.add_argument("--ignore-hours", action="store_true", help="Run even outside A-share hours")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Feishu alerts or write radar_alerts.jsonl")
    parser.add_argument("--max-seconds", type=float, default=None, help="Run bounded duration then exit cleanly")
    asyncio.run(run_ws_stream(parser.parse_args()))


if __name__ == "__main__":
    main()
