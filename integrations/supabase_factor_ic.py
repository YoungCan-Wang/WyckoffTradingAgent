"""因子 IC 评估结果落库。

只写 factor_ic_daily 一张表，按 (eval_date, factor_name, horizon, segment) upsert，
重跑同一天会覆盖而非累积。表 DDL 见 docs/sql/factor_ic_daily.sql——Python SDK 走 REST
不支持 DDL，需在 Supabase SQL Editor 手工执行一次。

落库的意义在于跨期对比：IC 单期值不构成证据，关键是 ic_ir 与方向一致性。
2026-08-22 首轮 19 个因子-前瞻组合在 3 段样本上方向全一致且全为负，是当日唯一
跨段稳定的结论；逐周留档才能看出方向何时开始变化。
"""

from __future__ import annotations

import logging
from datetime import date

from core.constants import TABLE_FACTOR_IC_DAILY
from integrations.supabase_base import create_admin_client

logger = logging.getLogger(__name__)
CHUNK = 200


def save_factor_ic_rows(
    rows: list[dict],
    *,
    eval_date: date | None = None,
    segment: str = "full",
    window_start: str = "",
    window_end: str = "",
    weights: dict[str, float] | None = None,
) -> int:
    """写入一批 IC 结果，返回成功行数。失败只 warning——评估本身已在日志里输出。"""
    if not rows:
        return 0
    stamp = (eval_date or date.today()).isoformat()
    weights = weights or {}
    payload = [
        {
            "eval_date": stamp,
            "factor_name": row["name"],
            "horizon": int(row["horizon"]),
            "segment": segment,
            "window_start": window_start or None,
            "window_end": window_end or None,
            "days": int(row["days"]),
            "avg_universe": row.get("avg_universe"),
            "rank_ic": row.get("rank_ic"),
            "ic_std": row.get("ic_std"),
            "ic_ir": row.get("ic_ir"),
            "positive_ratio": row.get("positive_ratio"),
            "monotonicity": row.get("monotonicity"),
            "verdict": row.get("verdict"),
            "useful": bool(row.get("useful")),
            "sign": int(row.get("sign") or 0),
            "suggested_weight": weights.get(row["name"]),
        }
        for row in rows
    ]
    client = create_admin_client()
    written = 0
    for start in range(0, len(payload), CHUNK):
        batch = payload[start : start + CHUNK]
        try:
            client.table(TABLE_FACTOR_IC_DAILY).upsert(
                batch, on_conflict="eval_date,factor_name,horizon,segment"
            ).execute()
            written += len(batch)
        except Exception as exc:  # noqa: BLE001 - 落库失败不应中断评估
            logger.warning("factor_ic 落库失败（%d 行）: %s", len(batch), str(exc)[:200])
    return written
