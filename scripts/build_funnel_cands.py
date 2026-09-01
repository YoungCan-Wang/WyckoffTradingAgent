"""从 signal_observations 导出漏斗候选集，供 evaluate_funnel_effect.py 使用。

原先这一步是临时脚本，正是因此埋了个口径错误：按 ``candidate_status == "formal_l4"``
建 L4 集合，漏掉 104 只 stage 已知（状态位被 ``Accum_B``/``Accum_C`` 顶掉）的正式
候选，且把它们算进了对照池。L4 成员判定只认 ``candidate_lane in FORMAL_L4_LANES``。

用法：
    python scripts/build_funnel_cands.py --output /tmp/funnel_cands.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Any

import _bootstrap  # noqa: F401

from core.candidate_metadata import code6
from core.candidate_tracks import FORMAL_L4_LANES

PAGE = 1000
COLUMNS = "trade_date,code,candidate_lane,candidate_status,regime"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出漏斗候选集")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--start", default="", help="起始交易日 YYYY-MM-DD，留空取全部")
    parser.add_argument("--end", default="", help="结束交易日 YYYY-MM-DD，留空取全部")
    return parser.parse_args()


def fetch_rows(start: str, end: str) -> list[dict[str, Any]]:
    from supabase import create_client

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SystemExit("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    client = create_client(url, key)
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = client.table("signal_observations").select(COLUMNS)
        if start:
            query = query.gte("trade_date", start)
        if end:
            query = query.lte("trade_date", end)
        page = query.range(offset, offset + PAGE - 1).execute().data or []
        rows += page
        if len(page) < PAGE:
            return rows
        offset += PAGE


def build(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    wide: dict[str, set[str]] = defaultdict(set)
    l4: dict[str, set[str]] = defaultdict(set)
    regime: dict[str, str] = {}
    for row in rows:
        date = str(row.get("trade_date") or "")[:10]
        code = code6(row.get("code"))
        if not date or not code:
            continue
        wide[date].add(code)
        if str(row.get("candidate_lane") or "").strip() in FORMAL_L4_LANES:
            l4[date].add(code)
        regime.setdefault(date, str(row.get("regime") or ""))
    return {
        date: {"regime": regime.get(date, ""), "formal_l4": sorted(l4[date]), "all": sorted(codes)}
        for date, codes in sorted(wide.items())
    }


def main() -> None:
    args = parse_args()
    out = build(fetch_rows(args.start, args.end))
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    l4_total = sum(len(day["formal_l4"]) for day in out.values())
    wide_total = sum(len(day["all"]) for day in out.values())
    print(f"{len(out)} 个交易日，宽池 {wide_total} 行，正式 L4 {l4_total} 行 -> {args.output}")


if __name__ == "__main__":
    main()
