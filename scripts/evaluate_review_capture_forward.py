"""算「复盘漏掉的票在 T+h 上赚不赚钱」，出 JSON + Markdown。

用法::

    python scripts/evaluate_review_capture_forward.py \
        --out docs/evidence/review_capture_forward

读 ``review_capture_daily``，按 stage 档位分桶，两种入场口径（T 日开盘 / T+1 开盘）
各出 h∈{1,2,3,5,10} 的绝对收益与同动量对照。为什么要两档、为什么 T 日开盘那档的
对照必须拒绝，见 ``workflows/review_capture_forward`` 的模块 docstring。

行情面板从 tushare 现取（本地没有 hist_full 快照）。取数范围要在复盘日两头各留出
余量：前面 ~25 个交易日给 mom20 的 shift(20) 预热，后面 ~11 天给 T+1+10 的卖出价。
预热不够会让最早那几天没有动量、直接从配对样本里掉出去，而这**不报错**——只是
可评估日静默少几天（见 memory start-is-not-the-eval-window）。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from core.funnel_effect_panels import DEFAULT_MIN_AMOUNT_WAN, build_panels, normalize_market_frame
from integrations.supabase_review_capture import load_capture_rows
from integrations.tushare_client import get_pro, has_tushare_token
from workflows.review_capture_forward import (
    FORWARD_HORIZONS,
    capture_forward_summary,
    forward_verdict_lines,
)

logger = logging.getLogger(__name__)

# mom20 要 shift(20)，再留几天缓冲；尾部要够 T+1+max(h) 的卖出价。
WARMUP_TRADING_DAYS = 30
TAIL_TRADING_DAYS = max(FORWARD_HORIZONS) + 3
# 自然日 ≈ 交易日 * 1.45（周末与节假日）。宁可多取。
CALENDAR_FACTOR = 1.5


def _fetch_market(codes: set[str], start: str, end: str) -> pd.DataFrame:
    """按交易日批量取，而不是按股票循环——后者是几千次调用，跑不动也超限。"""
    pro = get_pro()
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    dates = sorted(str(d) for d in cal["cal_date"])
    frames: list[pd.DataFrame] = []
    for trade_date in dates:
        daily = pro.daily(trade_date=trade_date)
        if daily is None or daily.empty:
            continue
        frames.append(daily[["ts_code", "trade_date", "open", "close", "amount"]])
    if not frames:
        raise SystemExit("tushare 未返回任何行情，无法构建面板")
    frame = pd.concat(frames, ignore_index=True)
    frame["code6"] = frame.ts_code.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    # 只留复盘涉及的票 + 对照池需要的全市场票：对照要在全市场里找同动量，故不能只留复盘票。
    logger.info("行情 %d 行，覆盖 %d 个交易日，复盘票 %d 只", len(frame), len(dates), len(codes))
    return frame.drop(columns=["code6"])


def _range(dates: list[str]) -> tuple[str, str]:
    """复盘日区间两头各扩出预热与卖出窗口，返回 tushare 的 YYYYMMDD。"""
    first = pd.Timestamp(dates[0]) - pd.Timedelta(days=int(WARMUP_TRADING_DAYS * CALENDAR_FACTOR))
    last = pd.Timestamp(dates[-1]) + pd.Timedelta(days=int(TAIL_TRADING_DAYS * CALENDAR_FACTOR))
    return first.strftime("%Y%m%d"), last.strftime("%Y%m%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="复盘捕获表的 T+h 前瞻收益评估")
    parser.add_argument("--since", default="", help="起始复盘日 YYYY-MM-DD")
    parser.add_argument("--until", default="", help="结束复盘日 YYYY-MM-DD")
    parser.add_argument("--out", default="", help="输出前缀，生成 .json 与 .md")
    parser.add_argument("--min-amount-wan", type=float, default=DEFAULT_MIN_AMOUNT_WAN)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    rows = load_capture_rows(args.since, args.until)
    logger.info("捕获表读回 %d 行", len(rows))
    if not rows:
        # 空与「被 RLS 挡住」同形，故把排查方向写进输出而不是只报空。
        logger.warning("捕获表无数据；若确信有数据，先确认用的是 service key 再重读")

    dates = sorted({str(r.get("trade_date") or "")[:10] for r in rows if r.get("trade_date")})
    panels = None
    if dates and has_tushare_token():
        start, end = _range(dates)
        codes = {str(r.get("ts_code") or "") for r in rows}
        panels = build_panels(
            normalize_market_frame(_fetch_market(codes, start, end)),
            min_amount_wan=args.min_amount_wan,
        )
    elif dates:
        logger.warning("无 tushare token，无法构建面板")

    summary = capture_forward_summary(rows, panels)
    lines = forward_verdict_lines(summary)
    text = "\n".join(lines)
    print(text)

    if args.out:
        base = Path(args.out)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        base.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
        logger.info("已写出 %s.json / %s.md", base, base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
