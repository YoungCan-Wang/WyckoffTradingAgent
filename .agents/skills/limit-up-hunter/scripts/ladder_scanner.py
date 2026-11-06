"""Real-time A-share limit-up ladder, deep-water reversal, and trap scanner."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from integrations.tickflow_client import TickFlowClient  # noqa: E402


@dataclass(frozen=True)
class QuoteMetrics:
    symbol: str
    name: str
    prev_close: float
    open_price: float
    last_price: float
    high_price: float
    low_price: float
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


def parse_quote(q: dict[str, Any]) -> QuoteMetrics | None:
    ext = q.get("ext") or {}
    name = str(ext.get("name") or "")
    if not name or "ST" in name:
        return None
    sym = str(q.get("symbol") or "")
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
    open_pct = (open_p - prev) / prev * 100.0
    last_pct = float(ext.get("change_pct") or 0.0) * 100.0
    high_pct = (high_p - prev) / prev * 100.0
    low_pct = (low_p - prev) / prev * 100.0
    is_20cm = sym.startswith(("30", "68"))
    lu_thresh = 19.8 if is_20cm else 9.8
    is_lu = last_pct >= lu_thresh and not sym.endswith(".BJ")
    is_ow = is_lu and open_pct >= (lu_thresh - 0.3) and low_pct >= (lu_thresh - 0.5)

    return QuoteMetrics(
        symbol=sym,
        name=name,
        prev_close=prev,
        open_price=open_p,
        last_price=last_p,
        high_price=high_p,
        low_price=low_p,
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
    )


def classify_ladder_buckets(metrics: list[QuoteMetrics], keyword: str | None = None) -> dict[str, list[QuoteMetrics]]:
    one_words = sorted([m for m in metrics if m.is_one_word], key=lambda x: x.amount_yi, reverse=True)
    turnover_lu = sorted(
        [m for m in metrics if m.is_limit_up and not m.is_one_word],
        key=lambda x: x.diff_from_open,
        reverse=True,
    )
    deep_reversals = sorted(
        [
            m
            for m in metrics
            if not m.is_limit_up
            and m.open_pct <= -2.0
            and m.diff_from_open >= 5.5
            and m.last_pct >= 0.0
            and m.last_price >= m.vwap
            and m.amount_yi >= 0.5
        ],
        key=lambda x: x.diff_from_open,
        reverse=True,
    )
    traps = sorted(
        [
            m
            for m in metrics
            if not m.is_limit_up and m.high_pct >= 6.0 and m.drop_from_high <= -4.5 and m.last_price < m.vwap
        ],
        key=lambda x: x.drop_from_high,
    )
    kw_matches = (
        sorted([m for m in metrics if keyword in m.name], key=lambda x: x.last_pct, reverse=True) if keyword else []
    )
    return {
        "one_words": one_words,
        "turnover_lu": turnover_lu,
        "deep_reversals": deep_reversals,
        "traps": traps,
        "keyword_matches": kw_matches,
    }


def tag_keyword_candidate(m: QuoteMetrics, front_lu_count: int, now_hhmm: str) -> str:
    if m.is_limit_up:
        return "[🔥前排封板先锋]"
    if m.last_price < m.vwap and m.drop_from_high <= -4.5:
        return "[❌冲高破均线拉黑]"
    if front_lu_count >= 3 and now_hhmm >= "10:25" and -1.5 <= m.last_pct <= 2.5:
        return "[⚠️10:30后平盘诱多禁入]"
    if m.last_price >= m.vwap and m.last_pct >= 3.0:
        return "[✅均线上活跃前排]"
    return "[⏸️后排观察/禁追]"


def format_row(m: QuoteMetrics, extra_tag: str = "") -> str:
    vwap_tag = ">VWAP" if m.last_price >= m.vwap else "<VWAP"
    suffix = f" {extra_tag}" if extra_tag else ""
    return (
        f"  {m.symbol:10s} {m.name:8s} | Open:{m.open_pct:+6.2f}% Low:{m.low_pct:+6.2f}% "
        f"-> Now:{m.last_pct:+6.2f}% ({m.last_price:6.2f}, {vwap_tag}:{m.vwap:6.2f}) "
        f"| High:{m.high_pct:+6.2f}% | FromOpen:{m.diff_from_open:+6.2f}% | Amt:{m.amount_yi:5.2f}亿{suffix}"
    )


def print_sections(metrics: list[QuoteMetrics], keyword: str | None, now_hhmm: str) -> None:
    buckets = classify_ladder_buckets(metrics, keyword)
    print(f"=== [1] 一字涨停顶死 ({len(buckets['one_words'])}只) ===")
    for m in buckets["one_words"]:
        print(format_row(m))

    print(f"\n=== [2] 换手真封死涨停 ({len(buckets['turnover_lu'])}只, 按日内弱转强幅度排序) ===")
    for m in buckets["turnover_lu"]:
        print(format_row(m))

    print(f"\n=== [3] 深水穿云箭候选 (Open<=-2%, 拉升>=+5.5%, 站稳VWAP之上: {len(buckets['deep_reversals'])}只) ===")
    for m in buckets["deep_reversals"][:20]:
        print(format_row(m))

    print(f"\n=== [4] 冲高回落破VWAP黑名单 (摸高>=+6%后回撤>=4.5%且破均线, 严禁抄底: {len(buckets['traps'])}只) ===")
    for m in buckets["traps"][:20]:
        print(format_row(m))

    if keyword:
        kw_list = buckets["keyword_matches"]
        lu_cnt = sum(1 for m in kw_list if m.is_limit_up)
        print(f"\n=== [5] 关键词『{keyword}』字辈/主题全扫描 ({len(kw_list)}只, 涨停{lu_cnt}只) ===")
        for m in kw_list:
            print(format_row(m, tag_keyword_candidate(m, lu_cnt, now_hhmm)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan A-share limit-up ladder and reversals.")
    parser.add_argument("--keyword", type=str, default=None, help="Filter prefix/theme keyword.")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("TICKFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("Missing TICKFLOW_API_KEY in environment.")

    client = TickFlowClient(api_key)
    qmap = client.get_quotes(universes=["CN_Equity_A"])
    parsed = [m for q in qmap.values() if (m := parse_quote(q)) is not None]
    now = datetime.now()
    print(f"Snapshot Time: {now.strftime('%Y-%m-%d %H:%M:%S')} | Valid Non-ST A-Shares: {len(parsed)}\n")
    print_sections(parsed, args.keyword, now.strftime("%H:%M"))


if __name__ == "__main__":
    main()
