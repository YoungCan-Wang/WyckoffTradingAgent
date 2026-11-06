"""Real-time A-share limit-up ladder, deep-water reversal, and observation scanner."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from integrations.tickflow_client import TickFlowClient  # noqa: E402

SH_TZ = ZoneInfo("Asia/Shanghai")
VWAP_TOL = 1e-3
PRICE_TOL = 1e-4


@dataclass(frozen=True)
class FiveMinSummary:
    first_5m_surge_pct: float
    first_5m_red: bool
    min_vol_ratio: float
    high_morning: float
    broke_morning_high: bool
    bars_since_high: int

    def format_tag(self) -> str:
        vol_str = f"缩量{self.min_vol_ratio * 100:.0f}%"
        if self.min_vol_ratio <= 0.15:
            vol_str += "(<=15%锁仓)"
        state = "过早高" if self.broke_morning_high else f"横盘{self.bars_since_high * 5}m"
        return f"[5m:首K{self.first_5m_surge_pct:+.1f}%|{vol_str}|{state}]"


@dataclass(frozen=True)
class QuoteMetrics:
    symbol: str
    name: str
    prev_close: float
    open_price: float
    last_price: float
    high_price: float
    low_price: float
    limit_up_price: float
    vwap: float
    open_pct: float
    last_pct: float
    high_pct: float
    low_pct: float
    diff_from_open: float
    drop_from_high: float
    amplitude: float
    amount_yi: float
    is_limit_up: bool
    is_one_word: bool
    prev_boards: int = 0
    m_days_n_boards: str = ""
    five_min: FiveMinSummary | None = None

    @property
    def above_vwap(self) -> bool:
        return self.last_price >= self.vwap - VWAP_TOL

    @property
    def ladder_label(self) -> str:
        if self.prev_boards >= 1:
            base = f"{self.prev_boards}进{self.prev_boards + 1}"
            if self.is_limit_up:
                base += "晋级"
            return f"{base}({self.m_days_n_boards})" if self.m_days_n_boards else base
        if self.m_days_n_boards:
            return f"反包({self.m_days_n_boards})"
        return "首板封板" if self.is_limit_up else "首板候选"


def calc_limit_up_price(prev_close: float, symbol: str) -> float:
    ratio = Decimal("1.20") if symbol.startswith(("30", "68")) else Decimal("1.10")
    base = Decimal(f"{prev_close:.2f}")
    return float((base * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_quote(q: dict[str, Any], ladder_info: dict[str, dict[str, Any]] | None = None) -> QuoteMetrics | None:
    sym = str(q.get("symbol") or "")
    if not sym or sym.endswith(".BJ"):
        return None
    ext = q.get("ext") or {}
    name = str(ext.get("name") or "").strip()
    if not name or "ST" in name or name.startswith(("N", "C")):
        return None
    prev = float(q.get("prev_close") or 0.0)
    open_p = float(q.get("open") or 0.0)
    last_p = float(q.get("last_price") or 0.0)
    high_p = float(q.get("high") or 0.0)
    low_p = float(q.get("low") or 0.0)
    if prev <= 0 or open_p <= 0 or last_p <= 0:
        return None

    vol = float(q.get("volume") or 0.0)
    amt = float(q.get("amount") or 0.0)
    vwap = amt / vol / 100.0 if vol > 0 else last_p
    lu_price = calc_limit_up_price(prev, sym)
    is_lu = last_p >= lu_price - PRICE_TOL
    is_ow = is_lu and open_p >= lu_price - PRICE_TOL and low_p >= lu_price - PRICE_TOL
    open_pct = (open_p - prev) / prev * 100.0
    last_pct = (last_p - prev) / prev * 100.0
    high_pct = (high_p - prev) / prev * 100.0
    low_pct = (low_p - prev) / prev * 100.0
    info = (ladder_info or {}).get(sym) or {}

    return QuoteMetrics(
        symbol=sym,
        name=name,
        prev_close=prev,
        open_price=open_p,
        last_price=last_p,
        high_price=high_p,
        low_price=low_p,
        limit_up_price=lu_price,
        vwap=vwap,
        open_pct=open_pct,
        last_pct=last_pct,
        high_pct=high_pct,
        low_pct=low_pct,
        diff_from_open=last_pct - open_pct,
        drop_from_high=last_pct - high_pct,
        amplitude=(high_p - low_p) / prev * 100.0,
        amount_yi=amt / 1e8,
        is_limit_up=is_lu,
        is_one_word=is_ow,
        prev_boards=int(info.get("prev_boards") or 0),
        m_days_n_boards=str(info.get("m_days_n_boards") or ""),
    )


def compute_ladder_from_daily_rows(
    rows_by_sym: dict[str, list[tuple[str, float, float]]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sym, rows in rows_by_sym.items():
        if sym.endswith(".BJ") or not rows:
            continue
        ordered = sorted(rows, key=lambda x: x[0])[-6:]
        flags = [prev > 0 and close >= calc_limit_up_price(prev, sym) - PRICE_TOL for _, prev, close in ordered]
        consec = 0
        for is_lu in reversed(flags):
            if is_lu:
                consec += 1
            else:
                break
        total_lu = sum(1 for f in flags if f)
        m_label = f"{len(ordered)}天{total_lu}板" if total_lu >= 3 and total_lu > consec else ""
        if consec >= 1 or m_label:
            result[sym] = {"prev_boards": consec, "m_days_n_boards": m_label}
    return result


def load_prev_ladder(snapshot_date: str, tushare_token: str | None) -> dict[str, dict[str, Any]]:
    cache_dir = Path.home() / ".wyckoff" / "cache"
    cache_file = cache_dir / f"prev_ladder_{snapshot_date}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not tushare_token:
        return {}
    try:
        import tushare as ts

        pro = ts.pro_api(tushare_token)
        cal = pro.trade_cal(exchange="SSE", end_date=snapshot_date, is_open="1", limit=10)
        dates = sorted(d for d in cal["cal_date"].tolist() if d < snapshot_date)[-6:]
        rows_by_sym: dict[str, list[tuple[str, float, float]]] = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            dfs = list(
                ex.map(
                    lambda d: (d, pro.daily(trade_date=d, fields="ts_code,close,pre_close")),
                    dates,
                )
            )
        for d, df in dfs:
            for row in df.itertuples(index=False):
                rows_by_sym.setdefault(str(row.ts_code), []).append((d, float(row.pre_close), float(row.close)))
        ladder = compute_ladder_from_daily_rows(rows_by_sym)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(ladder, ensure_ascii=False), encoding="utf-8")
        return ladder
    except Exception:
        return {}


def analyze_5m_bars(bars: Any, prev_close: float) -> FiveMinSummary | None:
    if bars is None or prev_close <= 0:
        return None
    records: list[dict[str, Any]] = bars.to_dict("records") if hasattr(bars, "to_dict") else list(bars)
    if not records:
        return None
    latest_date = str(records[-1].get("date") or records[-1].get("trade_date") or "")
    day_bars = (
        [b for b in records if str(b.get("date") or b.get("trade_date") or "") == latest_date]
        if latest_date
        else records
    )
    if not day_bars:
        return None
    b0 = day_bars[0]
    open_0 = float(b0.get("open") or 0.0)
    high_0 = float(b0.get("high") or 0.0)
    close_0 = float(b0.get("close") or 0.0)
    amt_0 = float(b0.get("amount") or b0.get("volume") or 0.0)
    first_surge = (high_0 - open_0) / prev_close * 100.0 if open_0 > 0 else 0.0
    consol_bars = [
        b
        for b in day_bars[1:6]
        if float(b.get("high") or 0.0) > float(b.get("low") or 0.0) + PRICE_TOL or float(b.get("open") or 0.0) <= 0.0
    ]
    subsequent_amts = [float(b.get("amount") or b.get("volume") or 0.0) for b in consol_bars]
    min_ratio = min(subsequent_amts) / amt_0 if (amt_0 > 0 and subsequent_amts) else 1.0
    high_morning = max(float(b.get("high") or 0.0) for b in day_bars[:3])
    highs = [float(b.get("high") or 0.0) for b in day_bars]
    max_high = max(highs)
    last_max_idx = max(idx for idx, h in enumerate(highs) if h >= max_high - PRICE_TOL)
    return FiveMinSummary(
        first_5m_surge_pct=first_surge,
        first_5m_red=close_0 >= prev_close - PRICE_TOL,
        min_vol_ratio=min_ratio,
        high_morning=high_morning,
        broke_morning_high=float(day_bars[-1].get("close") or 0.0) >= high_morning - PRICE_TOL,
        bars_since_high=len(day_bars) - 1 - last_max_idx,
    )


def extract_snapshot_time(quotes: Iterable[dict[str, Any]], fallback_now: datetime | None = None) -> datetime:
    max_ts = max((int(q.get("timestamp") or 0) for q in quotes), default=0)
    if max_ts > 0:
        return datetime.fromtimestamp(max_ts / 1000.0, tz=SH_TZ)
    return fallback_now or datetime.now(SH_TZ)


def _is_first_board_reversal(m: QuoteMetrics) -> bool:
    if m.is_limit_up or m.prev_boards > 0 or m.open_pct > -2.5 or not m.above_vwap or m.amount_yi < 1.0:
        return False
    is_20cm = m.symbol.startswith(("30", "68"))
    min_surge = 11.0 if is_20cm else 6.0
    min_last = 5.0 if is_20cm else 2.0
    return m.diff_from_open >= min_surge and m.last_pct >= min_last


def classify_ladder_buckets(metrics: list[QuoteMetrics], keyword: str | None = None) -> dict[str, list[QuoteMetrics]]:
    prev_pool = sorted(
        [m for m in metrics if m.prev_boards >= 1 or m.m_days_n_boards],
        key=lambda x: (x.prev_boards, x.last_pct),
        reverse=True,
    )
    one_words = sorted([m for m in metrics if m.is_one_word], key=lambda x: (x.prev_boards, x.amount_yi), reverse=True)
    turnover_lu = sorted(
        [m for m in metrics if m.is_limit_up and not m.is_one_word],
        key=lambda x: (x.prev_boards, x.diff_from_open),
        reverse=True,
    )
    ladder_reversals = sorted(
        [m for m in prev_pool if m.open_pct <= -2.5 and m.diff_from_open >= 6.0 and m.last_pct >= 0.0 and m.above_vwap],
        key=lambda x: (x.prev_boards, x.diff_from_open),
        reverse=True,
    )
    first_reversals = sorted(
        [m for m in metrics if _is_first_board_reversal(m)],
        key=lambda x: x.diff_from_open,
        reverse=True,
    )
    traps = sorted(
        [
            m
            for m in metrics
            if not m.is_limit_up and m.high_pct >= 6.0 and m.drop_from_high <= -4.5 and not m.above_vwap
        ],
        key=lambda x: (x.prev_boards, -x.drop_from_high),
        reverse=True,
    )
    kw_matches = (
        sorted([m for m in metrics if keyword in m.name], key=lambda x: x.last_pct, reverse=True) if keyword else []
    )
    return {
        "prev_ladder_pool": prev_pool,
        "one_words": one_words,
        "turnover_lu": turnover_lu,
        "ladder_reversals": ladder_reversals,
        "deep_reversals": ladder_reversals + first_reversals,
        "traps": traps,
        "keyword_matches": kw_matches,
    }


def tag_keyword_candidate(m: QuoteMetrics, front_lu_count: int, now_hhmm: str) -> str:
    if m.is_limit_up:
        return "[🔥前排封板先锋]"
    if not m.above_vwap and m.drop_from_high <= -4.5:
        return "[❌冲高破均线观察]"
    if front_lu_count >= 4 and now_hhmm >= "10:30" and 0.0 <= m.last_pct <= 2.5:
        return "[⚠️10:30后平盘观察]"
    if m.above_vwap and m.last_pct >= 3.0:
        return "[✅均线上活跃前排]"
    return "[⏸️后排跟风观察]"


def format_row(m: QuoteMetrics, extra_tag: str = "") -> str:
    vwap_tag = ">=VWAP" if m.above_vwap else "<VWAP"
    five_tag = f" {m.five_min.format_tag()}" if m.five_min else ""
    suffix = f" {extra_tag}" if extra_tag else ""
    return (
        f"  {m.symbol:10s} {m.name:8s} [{m.ladder_label:8s}] | Open:{m.open_pct:+6.2f}% Low:{m.low_pct:+6.2f}% "
        f"-> Now:{m.last_pct:+6.2f}% ({m.last_price:6.2f}, {vwap_tag}:{m.vwap:6.2f}) "
        f"| High:{m.high_pct:+6.2f}% | FromOpen:{m.diff_from_open:+6.2f}% | Amt:{m.amount_yi:5.2f}亿{five_tag}{suffix}"
    )


def print_sections(metrics: list[QuoteMetrics], keyword: str | None, now_hhmm: str) -> None:
    buckets = classify_ladder_buckets(metrics, keyword)
    if buckets["prev_ladder_pool"]:
        print(
            f"=== [0] 昨日涨停/连板池今日晋级全量监控 (100%保留不因低开剔除: {len(buckets['prev_ladder_pool'])}只) ==="
        )
        for m in buckets["prev_ladder_pool"][:35]:
            print(format_row(m))
        print()

    print(f"=== [1] 一字涨停顶死 ({len(buckets['one_words'])}只) ===")
    for m in buckets["one_words"]:
        print(format_row(m))

    print(f"\n=== [2] 换手真封死涨停 ({len(buckets['turnover_lu'])}只, 按连板高度与弱转强幅度排序) ===")
    for m in buckets["turnover_lu"]:
        print(format_row(m))

    print(
        f"\n=== [3] 深水穿云箭弱转强观察 (Open<=-2.5%, 10cm拉升>=+6%/20cm>=+11%, 站稳VWAP: {len(buckets['deep_reversals'])}只) ==="
    )
    for m in buckets["deep_reversals"][:20]:
        print(format_row(m))

    print(f"\n=== [4] 冲高回落破VWAP观察名单 (摸高>=+6%后回撤>=4.5%且破均线: {len(buckets['traps'])}只) ===")
    for m in buckets["traps"][:20]:
        print(format_row(m))

    if keyword:
        kw_list = buckets["keyword_matches"]
        lu_cnt = sum(1 for m in kw_list if m.is_limit_up)
        print(f"\n=== [5] 关键词『{keyword}』字辈/主题全扫描 ({len(kw_list)}只, 涨停{lu_cnt}只) ===")
        for m in kw_list:
            print(format_row(m, tag_keyword_candidate(m, lu_cnt, now_hhmm)))


def enrich_with_5m_bars(client: TickFlowClient, metrics: list[QuoteMetrics]) -> list[QuoteMetrics]:
    buckets = classify_ladder_buckets(metrics)
    targets = {
        m.symbol: m for m in (buckets["prev_ladder_pool"][:35] + buckets["deep_reversals"][:20] + buckets["traps"][:15])
    }
    if not targets:
        return metrics
    try:
        bars_map = client.get_intraday_batch(list(targets.keys()), period="5m", count=55)
    except Exception:
        return metrics
    enriched: list[QuoteMetrics] = []
    for m in metrics:
        bars = bars_map.get(m.symbol)
        summary = analyze_5m_bars(bars, m.prev_close) if bars is not None else None
        enriched.append(replace(m, five_min=summary) if summary else m)
    return enriched


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan A-share limit-up ladder and reversals.")
    parser.add_argument("--keyword", type=str, default=None, help="Filter prefix/theme keyword.")
    parser.add_argument("--no-5m", action="store_true", help="Skip 5-minute K-line verification.")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("TICKFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("Missing TICKFLOW_API_KEY in environment.")

    client = TickFlowClient(api_key)
    qmap = client.get_quotes(universes=["CN_Equity_A"])
    snap_dt = extract_snapshot_time(qmap.values())
    ladder_info = load_prev_ladder(snap_dt.strftime("%Y%m%d"), os.environ.get("TUSHARE_TOKEN"))
    parsed = [m for q in qmap.values() if (m := parse_quote(q, ladder_info)) is not None]
    if not args.no_5m:
        parsed = enrich_with_5m_bars(client, parsed)
    print(
        f"Snapshot Time (Asia/Shanghai): {snap_dt.strftime('%Y-%m-%d %H:%M:%S')} "
        f"| Valid Non-ST Main/ChiNext/STAR A-Shares: {len(parsed)} | Prev Ladder Pool: {len(ladder_info)}\n"
    )
    print_sections(parsed, args.keyword, snap_dt.strftime("%H:%M"))


if __name__ == "__main__":
    main()
