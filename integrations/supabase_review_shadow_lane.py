"""影子车道逐日观测落库。

只写 review_shadow_lane_daily 一张表,按 (trade_date, ts_code, lane) upsert,
重跑同一天覆盖而非累积。建表语句由
`python scripts/print_review_shadow_lane_ddl.py` 输出。

为什么必须落库:trace 只活在 `daily-job-artifacts-*` 里(retention-days: 30,与日志
同包),而且**补不回来**——回测引擎不重放漏斗分层,没有任何路径能从快照倒推出
「某日某票卡在哪一层、watch_score 多少」。每过一天没留存就永久少一天样本。

行来源只有 trace 一处:车道判定走 core.review_shadow_lanes,不在这里重新分类,
否则报告与落库两套口径会漂移(见 memory two-gates-must-share-one-source)。
"""

from __future__ import annotations

import logging
from typing import Any

from core.constants import TABLE_REVIEW_SHADOW_LANE_DAILY
from core.review_shadow_lanes import shadow_signal_from_decision
from integrations.supabase_base import create_admin_client, require_server_write_context

logger = logging.getLogger(__name__)
CHUNK = 200
_CONFLICT_KEY = "trade_date,ts_code,lane"


def build_lane_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从一份 review trace 抽出有影子车道的行。

    全市场约 5000 只里通常只有 100~200 只落在某条车道上,其余(过候选、数据失败、
    风控拦截)不是观测对象。只落这部分,表才小到能直接查。

    车道判定复用 shadow_signal_from_decision——与复盘报告、影子回放同一个函数。
    """
    trade_date = str(payload.get("trade_date") or "").strip()
    if not trade_date:
        return []
    policy = payload.get("policy") or {}
    gap_cap = _float(policy.get("shadow_near_l2_max_gap_pct"))
    rows: list[dict[str, Any]] = []
    for code, raw in (payload.get("symbols") or {}).items():
        row = dict(raw or {})
        signal = shadow_signal_from_decision(row, **({} if gap_cap is None else {"near_l2_max_gap_pct": gap_cap}))
        if signal is None:
            continue
        rows.append(_lane_row(trade_date, str(code), row, signal))
    return rows


def _lane_row(trade_date: str, code: str, row: dict[str, Any], signal: Any) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "ts_code": code,
        "name": _text(row.get("name")) or None,
        "sector": _text(row.get("sector")) or None,
        "lane": signal.lane,
        "stage": _text(row.get("stage")),
        "score": None if signal.score is None else round(float(signal.score), 4),
        "ranked": bool(signal.ranked),
        "reason": signal.reason,
        "watch_score": _float(row.get("layer3_quality_score")),
        "l2_channel": _text(row.get("l2_channel")) or None,
        "l1_eligible": bool(row.get("l1_eligible")),
        "l2_eligible": bool(row.get("l2_eligible")),
        "l3_eligible": bool(row.get("l3_eligible")),
        "rps_fast": _float(row.get("rps_fast")),
        "rps_slow": _float(row.get("rps_slow")),
        "close": _float(row.get("close")),
        "policy_version": signal.policy_version,
    }


def save_review_shadow_lane_rows(rows: list[dict[str, Any]]) -> int:
    """写入一批车道观测,返回成功行数。

    失败只 warning:漏斗主流程不该因为这张观测表写不进去而中断。但**不会**打印
    「已写入」这类成功句式——见 memory success-shaped-log-hid-total-loss,那让
    影子账本缺列停摆两个月没人发现。写了几行就说几行,零行显式说零行。
    """
    if not rows:
        return 0
    require_server_write_context(f"{TABLE_REVIEW_SHADOW_LANE_DAILY} write")
    client = create_admin_client()
    written = 0
    failed = 0
    for start in range(0, len(rows), CHUNK):
        batch = rows[start : start + CHUNK]
        try:
            client.table(TABLE_REVIEW_SHADOW_LANE_DAILY).upsert(batch, on_conflict=_CONFLICT_KEY).execute()
            written += len(batch)
        except Exception as exc:  # noqa: BLE001 - 观测表写失败不中断漏斗
            failed += len(batch)
            logger.warning("[shadow-lane] 批次写入失败 rows=%d: %s", len(batch), exc)
    if failed or written < len(rows):
        logger.warning(
            "[shadow-lane] %s 未全部写入: written=%d failed=%d total=%d",
            TABLE_REVIEW_SHADOW_LANE_DAILY,
            written,
            failed,
            len(rows),
        )
    else:
        logger.info("[shadow-lane] %s written=%d", TABLE_REVIEW_SHADOW_LANE_DAILY, written)
    return written


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _text(value: Any) -> str:
    return str(value or "").strip()
