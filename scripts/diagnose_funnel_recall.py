"""按指定代码回放漏斗，逐层给出「为什么没进候选池」的归因。

只读诊断：不写库、不发通知、不改配置。用于回答“某几只票为什么没被漏斗接住”，
在为召回缺口新建通道之前先确认现有各层的真实拒绝原因。

用法：
    python scripts/diagnose_funnel_recall.py 300502 300308 002463 601138 000938
    python scripts/diagnose_funnel_recall.py --date 2026-08-07 300502
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import _bootstrap  # noqa: F401


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按代码回放漏斗并逐层归因")
    parser.add_argument("codes", nargs="+", help="六位股票代码")
    parser.add_argument("--date", default="", help="信号日 YYYY-MM-DD，默认最近交易日")
    parser.add_argument("--json-out", default="", help="把结构化结果写入该路径")
    return parser.parse_args()


def _resolve_signal_date(raw: str) -> date:
    from integrations.fetch_a_share_csv import resolve_trading_window
    from utils.trading_clock import resolve_end_calendar_day

    end_day = date.fromisoformat(raw) if raw else resolve_end_calendar_day()
    return resolve_trading_window(end_calendar_day=end_day, trading_days=1).end_trade_date


def _replay(signal_date: date) -> tuple[dict, dict]:
    from workflows.review_list_replay import run_previous_funnel

    return run_previous_funnel(signal_date, log=print)


def _stage_rows(codes: list[str], ctx) -> list[dict[str, str]]:
    from workflows.review_list_replay import classify_review_code

    rows = []
    for code in codes:
        name, stage, reason = classify_review_code(code, ctx)
        rows.append({"code": code, "name": name, "stage": stage, "reason": reason})
    return rows


def _print_report(signal_date: date, end_trade_date: str, rows: list[dict[str, str]]) -> None:
    print(f"\n{'=' * 78}")
    print(f"漏斗召回归因  信号日={signal_date}  漏斗数据截止={end_trade_date}")
    print(f"{'=' * 78}")
    for row in rows:
        print(f"\n{row['code']} {row['name']}")
        print(f"  阶段: {row['stage']}")
        print(f"  原因: {row['reason']}")
    print(f"\n{'-' * 78}")
    counter: dict[str, int] = {}
    for row in rows:
        counter[row["stage"]] = counter.get(row["stage"], 0) + 1
    print("阶段分布: " + "、".join(f"{stage} {count}" for stage, count in sorted(counter.items())))


def main() -> int:
    args = _parse_args()
    codes = [str(c).strip() for c in args.codes if str(c).strip()]
    signal_date = _resolve_signal_date(args.date)
    print(f"[diagnose] 信号日 {signal_date}，回放 {len(codes)} 只: {', '.join(codes)}")

    triggers, metrics = _replay(signal_date)

    from workflows.review_list_replay import replay_context

    ctx = replay_context(triggers, metrics, log=print)
    if ctx is None:
        print("[diagnose] 缺少调试上下文，无法归因")
        return 3

    rows = _stage_rows(codes, ctx)
    _print_report(signal_date, ctx.end_trade_date, rows)

    if args.json_out:
        payload = {
            "signal_date": str(signal_date),
            "funnel_end_trade_date": ctx.end_trade_date,
            "rows": rows,
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"[diagnose] 结构化结果已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
