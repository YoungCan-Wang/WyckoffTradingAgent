"""卖出建议归因：跑数、落盘、推飞书。

回答一个问题：**系统建议卖出之后，那些票涨了还是跌了。**

2026-08-17 首轮（10 只票 / 20 次建议）：卖出后平均 +14.19%，同期上证 +2.01%，
即卖错约 13.7pct。两个机制假设都被数据否掉——「陈旧止损是主因」（偏离<5% 也错 17.36%）
与「重复触发是主因」（首次已错 14.74%）。20 次里 4 次挤在 2026-07-30 同一天，
而那天是阶段底部，故更像同一次市场时机误判的多次表现。

样本仅 10 只票、全落在同一段 V 型行情，**撑不起修改风控代码**。本脚本的作用是让样本
持续积累，判据留给未来的数据。

用法::

    python scripts/evaluate_exit_attribution.py --days 60
    python scripts/evaluate_exit_attribution.py --days 60 --no-notify
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd

from core.exit_attribution import (
    ExitRecord,
    as_report_dict,
    build_attribution,
    classify_origin,
    parse_stop_loss,
)

BENCHMARK = "000001"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="卖出建议归因")
    parser.add_argument("--days", type=int, default=60, help="回看天数")
    parser.add_argument("--portfolio-id", default="", help="限定组合，留空则全部")
    parser.add_argument("--out", default="artifacts/evidence", help="输出目录")
    parser.add_argument("--no-notify", action="store_true", help="不推飞书")
    return parser.parse_args()


def _load_sell_orders(days: int, portfolio_id: str) -> pd.DataFrame:
    from integrations.supabase_base import create_admin_client, is_admin_configured

    if not is_admin_configured():
        raise SystemExit("需要 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    client = create_admin_client()
    query = client.table("trade_orders").select("*").order("created_at")
    if portfolio_id:
        query = query.eq("portfolio_id", portfolio_id)
    frame = pd.DataFrame(query.execute().data or [])
    if frame.empty:
        return frame
    frame["trade_date"] = frame.created_at.astype(str).str[:10]
    cutoff = (pd.Timestamp.now() - pd.Timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")
    sells = frame[(frame.action.isin(["EXIT", "TRIM"])) & (frame.trade_date >= cutoff)]
    return sells.sort_values("trade_date")


def _price_series(code: str, start: str):
    from integrations.data_source import fetch_stock_hist

    frame = fetch_stock_hist(code, start, pd.Timestamp.now().strftime("%Y-%m-%d"))
    if frame is None or frame.empty:
        return None
    work = frame.rename(columns={"日期": "date", "收盘": "close"})
    if "close" not in work.columns or "date" not in work.columns:
        return None
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["close"])
    work["ds"] = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
    return work.sort_values("ds").reset_index(drop=True)


def _benchmark_series(start: str):
    from integrations.index_data_source import fetch_index_hist

    frame = fetch_index_hist(BENCHMARK, start, pd.Timestamp.now().strftime("%Y-%m-%d"))
    if frame is None or frame.empty:
        return None
    work = frame.copy()
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = work.dropna(subset=["close"])
    date_col = "date" if "date" in work.columns else work.columns[0]
    work["ds"] = pd.to_datetime(work[date_col]).dt.strftime("%Y-%m-%d")
    return work.sort_values("ds").drop_duplicates("ds").reset_index(drop=True)


def collect_records(sells: pd.DataFrame, start: str) -> list[ExitRecord]:
    bench = _benchmark_series(start)
    records: list[ExitRecord] = []
    for code, group in sells.groupby("code"):
        series = _price_series(str(code), start)
        if series is None:
            continue
        for sequence, (_, row) in enumerate(group.sort_values("trade_date").iterrows(), start=1):
            price = float(row.get("price_hint") or 0.0)
            if price <= 0:
                continue
            trade_date = str(row.trade_date)
            forward = series[series.ds >= trade_date]
            if forward.empty:
                continue
            after = (float(forward.close.iloc[-1]) / price - 1.0) * 100.0
            benchmark_pct = None
            if bench is not None:
                # 基准区间用个股实际的首末交易日对齐，避开周末的下单日期。
                window = bench[(bench.ds >= forward.ds.iloc[0]) & (bench.ds <= forward.ds.iloc[-1])]
                if len(window) > 1:
                    benchmark_pct = (float(window.close.iloc[-1]) / float(window.close.iloc[0]) - 1.0) * 100.0
            records.append(
                ExitRecord(
                    code=str(code),
                    name=str(row.get("name") or ""),
                    action=str(row.get("action") or ""),
                    trade_date=trade_date,
                    price=price,
                    sequence=sequence,
                    origin=classify_origin(row.get("reason")),
                    stop_loss=parse_stop_loss(row.get("reason")),
                    after_pct=after,
                    benchmark_pct=benchmark_pct,
                )
            )
    return records


def render_markdown(report_dict: dict, records: list[ExitRecord]) -> str:
    overall = report_dict.get("overall") or {}
    lines = [
        f"**样本** {report_dict['records']} 次建议 / {report_dict['codes']} 只票",
        f"**整体** 卖出后 {overall.get('after_pct', 0):+.2f}%　超额 {overall.get('excess_pct') or 0:+.2f}pct　"
        f"卖对 {overall.get('sold_correctly', 0)}/{overall.get('n', 0)}",
        "",
        "| 分组 | n | 卖出后 | 超额 | 卖对 |",
        "| --- | --: | --: | --: | --: |",
    ]
    for key in ("by_origin", "by_sequence", "by_stop_band"):
        for stat in report_dict.get(key) or []:
            after = stat.get("after_pct")
            excess = stat.get("excess_pct")
            lines.append(
                f"| {stat['label']} | {stat['n']} | "
                f"{'—' if after is None else f'{after:+.2f}%'} | "
                f"{'—' if excess is None else f'{excess:+.2f}'} | {stat['sold_correctly']}/{stat['n']} |"
            )
    lines += [
        "",
        "**读法**　卖出后为负=卖对了（避开下跌），为正=卖早了。超额已扣同期上证。",
        "",
        "**接下来做什么**",
        "- ① 超额显著为正意味着卖出建议系统性偏晚，此时**不要**执行 EXIT/清仓建议。",
        "- ② 不据此改风控代码：样本少且集中在同一段行情，机制假设（陈旧止损、重复触发）"
        "首轮均被数据否掉，更像市场时机误判而非机制缺陷。",
        "- ③ 下一步只做一件事：继续积累样本，跨越不同市场环境后再重判。",
    ]
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    start = (pd.Timestamp.now() - pd.Timedelta(days=max(int(args.days), 1) + 30)).strftime("%Y-%m-%d")
    sells = _load_sell_orders(args.days, args.portfolio_id.strip())
    if sells.empty:
        print("[exit] 区间内没有 EXIT/TRIM 建议")
        return 0
    records = collect_records(sells, start)
    if not records:
        print("[exit] 无法关联行情，样本为空")
        return 1
    report = as_report_dict(build_attribution(records))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "exit_attribution.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report, records)
    print(markdown)
    if not args.no_notify:
        _notify(markdown)
    return 0


def _notify(markdown: str) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("[exit] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    from utils.feishu import send_feishu_notification

    title = f"卖出建议归因｜{pd.Timestamp.now().strftime('%Y-%m-%d')}"
    print("[exit] feishu sent" if send_feishu_notification(webhook, title, markdown) else "[exit] feishu failed")


if __name__ == "__main__":
    raise SystemExit(main())
