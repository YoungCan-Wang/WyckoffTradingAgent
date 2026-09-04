"""强势股复盘捕获率逐日落库。

只写 review_capture_daily 一张表,按 (trade_date, ts_code) upsert,重跑同一天覆盖
而非累积。建表语句由 `python scripts/print_review_capture_ddl.py` 输出。

为什么必须落库:复盘行依赖前一日 trace(哪只票卡在哪一层),trace 只活在
`daily-job-artifacts-*` 里(retention-days: 30),复盘自己的产物在
`review-list-replay-logs-*` 里只留 7 天。两个都过期后那一天就永久算不出来,
而单日样本又没有判别力(2026-09-03:捕获率 p=0.740,题材闸增量 p=0.851),
必须靠 20+ 个交易日累积。每过一天没留存就永久少一天样本。

行来源只有 build_replay_rows 一处:档位归因不在这里重新分类,否则报告与落库
两套口径会漂移(见 memory two-gates-must-share-one-source)。
"""

from __future__ import annotations

import logging
from typing import Any

from core.constants import TABLE_REVIEW_CAPTURE_DAILY
from core.funnel_taxonomy import REVIEW_STAGE_CANDIDATE_HIT
from integrations.supabase_base import create_admin_client, require_server_write_context

logger = logging.getLogger(__name__)
CHUNK = 200
_CONFLICT_KEY = "trade_date,ts_code"


def build_capture_rows(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    previous_trade_date: str,
    denominators: dict[str, int],
    gain_map: dict[str, float | None] | None = None,
    context_source: str = "",
) -> list[dict[str, Any]]:
    """把复盘行转成落库行。

    ``denominators`` 是当日的五个分母(universe/l1/l2/l3/candidate)。基准率必须
    逐日算再合并:复盘池按「今日 >7% 且前一日 <3%」选出来,本身就是按结果选样,
    只有拿同一天的同层分母作对照,召回率才有意义。所以分母跟着每一行落下来,
    而不是事后去 join 一张可能已经过期的 trace。
    """
    if not trade_date or not previous_trade_date:
        return []
    gains = gain_map or {}
    pool_size = len(rows)
    out: list[dict[str, Any]] = []
    for raw in rows:
        code = _text((raw or {}).get("code"))
        if not code:
            continue
        out.append(
            {
                "trade_date": trade_date,
                "previous_trade_date": previous_trade_date,
                "ts_code": code,
                "name": _text(raw.get("name")) or None,
                "stage": _text(raw.get("stage")),
                "reason": _text(raw.get("reason")) or None,
                "l1_eligible": bool(raw.get("l1_eligible")),
                "l2_eligible": bool(raw.get("l2_eligible")),
                "l3_eligible": bool(raw.get("l3_eligible")),
                "is_candidate": bool(raw.get("stage") == REVIEW_STAGE_CANDIDATE_HIT),
                "trigger_labels": [str(x) for x in (raw.get("trigger_labels") or [])],
                "risk_signal": _text(raw.get("risk_signal")) or None,
                "tracked_previous_day": bool(raw.get("tracked_previous_day")),
                "ai_recommended_previous_day": bool(raw.get("ai_recommended_previous_day")),
                "shadow_lane": _text(raw.get("shadow_lane")) or None,
                "shadow_score": _round(raw.get("shadow_score")),
                "open_executable": bool(raw.get("open_executable")),
                "intraday_executable": bool(raw.get("intraday_executable")),
                "open_gap_pct": _round(raw.get("open_gap_pct")),
                "gain_pct": _round(gains.get(code)),
                "pool_size": pool_size,
                "universe_count": int(denominators.get("universe") or 0),
                "l1_count": int(denominators.get("l1") or 0),
                "l2_count": int(denominators.get("l2") or 0),
                "l3_count": int(denominators.get("l3") or 0),
                "candidate_count": int(denominators.get("candidate") or 0),
                "context_source": _text(context_source) or None,
            }
        )
    return out


def save_review_capture_rows(rows: list[dict[str, Any]]) -> int:
    """写入一批复盘捕获行,返回成功行数。

    失败只 warning:复盘主流程不该因为这张观测表写不进去而中断(飞书报告已经发了)。
    但**不会**打印「已写入」这类成功句式——见 memory success-shaped-log-hid-total-loss,
    那让影子账本缺列停摆两个月没人发现。写了几行就说几行,零行显式说零行。
    """
    if not rows:
        return 0
    require_server_write_context(f"{TABLE_REVIEW_CAPTURE_DAILY} write")
    client = create_admin_client()
    written = 0
    failed = 0
    for start in range(0, len(rows), CHUNK):
        batch = rows[start : start + CHUNK]
        try:
            client.table(TABLE_REVIEW_CAPTURE_DAILY).upsert(batch, on_conflict=_CONFLICT_KEY).execute()
            written += len(batch)
        except Exception as exc:  # noqa: BLE001 - 观测表写失败不中断复盘
            failed += len(batch)
            logger.warning("[review-capture] 批次写入失败 rows=%d: %s", len(batch), exc)
    if failed or written < len(rows):
        logger.warning(
            "[review-capture] %s 未全部写入: written=%d failed=%d total=%d",
            TABLE_REVIEW_CAPTURE_DAILY,
            written,
            failed,
            len(rows),
        )
    else:
        logger.info("[review-capture] %s written=%d", TABLE_REVIEW_CAPTURE_DAILY, written)
    return written


def _round(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else round(result, 4)


def _text(value: Any) -> str:
    return str(value or "").strip()
