"""Database maintenance workflow for expiring old Supabase rows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.constants import (
    TABLE_CONCEPT_HEAT_HISTORY,
    TABLE_DAILY_NAV,
    TABLE_EXTERNAL_SEED_OBSERVATIONS,
    TABLE_FACTOR_IC_DAILY,
    TABLE_MARKET_SIGNAL_DAILY,
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_RECOMMENDATION_TRACKING_HK,
    TABLE_RECOMMENDATION_TRACKING_US,
    TABLE_REVIEW_SHADOW_LANE_DAILY,
    TABLE_SIGNAL_HEALTH_DAILY,
    TABLE_SIGNAL_OBSERVATIONS,
    TABLE_SIGNAL_OUTCOMES,
    TABLE_SIGNAL_PENDING,
    TABLE_SIGNAL_POLICY_SHADOW_RUNS,
    TABLE_STRATEGY_ATTRIBUTION_REPORTS,
    TABLE_THEME_RADAR_SNAPSHOT,
    TABLE_TRADE_ORDERS,
)
from integrations.supabase_base import create_admin_client


@dataclass(frozen=True)
class DbMaintenanceRequest:
    dry_run: bool = False


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(float(raw)), minimum)
    except (TypeError, ValueError):
        return default


# (table, date_column, ttl_days, cutoff_kind)
# cutoff_kind:
# - iso_date:      YYYY-MM-DD（字符串日期列）
# - yyyymmdd_int:  YYYYMMDD（整数日期列）
CLEANUP_RULES: list[tuple[str, str, int, str]] = [
    (TABLE_TRADE_ORDERS, "trade_date", 15, "iso_date"),
    (TABLE_SIGNAL_PENDING, "signal_date", _int_env("DB_SIGNAL_PENDING_RETENTION_DAYS", 30), "iso_date"),
    (TABLE_MARKET_SIGNAL_DAILY, "trade_date", _int_env("DB_MARKET_SIGNAL_RETENTION_DAYS", 180), "iso_date"),
    (TABLE_DAILY_NAV, "trade_date", 15, "iso_date"),
    (TABLE_EXTERNAL_SEED_OBSERVATIONS, "trade_date", _int_env("FUNNEL_EXTERNAL_SEED_RETENTION_DAYS", 180), "iso_date"),
    # 因子 IC 每周一行/因子，量很小；留 365 天以便观察方向是否跨年稳定——
    # 该表的价值恰在长期趋势，删太早就失去意义。
    (TABLE_FACTOR_IC_DAILY, "eval_date", _int_env("DB_FACTOR_IC_RETENTION_DAYS", 365), "iso_date"),
    # ↓ 2026-09-14 新增。此前 29 张表只有 9 张有规则，下面 7 张是按「实测字节」而非行数挑的：
    #   全库约 79MB，strategy_attribution_reports 52 行就占 32.5MB(41%)，单行 870KB。
    #
    # 归因报告：**每交易日一行**，当前单行约 870KB，一年约 213MB —— 全库最大的字节增长源。
    # 而且单行还在长：2026-07-02 是 99KB，2026-07-17 起跳到 586KB，到 2026-09-11 已 870KB。
    # 长的是四个 JSON 列(signal_context_stats 0→236KB、recommendations 2→223KB、
    # shadow_diff 10→216KB、score_bucket 48→147KB)，它们随候选池规模走，TTL 只能封住总量，
    # 治不了单行变大——真要治得去瘦身 payload，那是另一件事。
    #
    # 取 30 天而不是更长：三个消费者没有一个需要历史。
    #   workflows/strategy_attribution_policy.py 的 load_latest_attribution_report 是 limit(1)，
    #   web/apps/web/src/routes/attribution.tsx 的 fetchLatestReport 是 limit(1)，
    #   web/packages/shared/src/chat-tools.ts 的 execQueryAttribution 按 report_date 倒序取 limit N
    #   (N 由模型给，实际是个位数)。
    # 报告自身覆盖 30 天滚动窗(strategy_attribution_report.py 的 days=30)，所以留 30 天日历日
    # 恰好握着两段不重叠的窗:最新那份看 -30..0，留存边界上那份看 -60..-30，够做「本期 vs 上期」。
    (
        TABLE_STRATEGY_ATTRIBUTION_REPORTS,
        "report_date",
        _int_env("DB_ATTRIBUTION_RETENTION_DAYS", 30),
        "iso_date",
    ),
    # 信号健康度：**派生表**，由 signal_outcomes 经 signal_feedback_job.py 每日整体重写
    # (replace_signal_health 按 market+as_of_date upsert 后删孤行)。生产只读最新一份——
    # load_signal_health_snapshot 按 as_of_date 倒序取样再 _latest_rows 去重，历史份数不参与决策。
    # 读历史的只有 scripts/evaluate_capital_context_alpha.py 这个一次性 eval，不在任何 workflow 里。
    # 删了也能从 outcomes 重算，所以这张表不需要长期留存。
    (TABLE_SIGNAL_HEALTH_DAILY, "as_of_date", _int_env("DB_SIGNAL_HEALTH_RETENTION_DAYS", 90), "iso_date"),
    # 影子车道：1171 行/交易日，全库行增长最快(≈164MB/年)。全仓没有任何 select——
    # integrations/supabase_review_shadow_lane.py 只有 upsert，score_direction 判定读的是
    # artifact 里的 review_trace_*.json.gz 而非这张表。留 180 天不是因为有程序依赖，而是因为
    # artifact 只活 90 天：这张表是那批证据唯一能活过 90 天的副本，供事后用 SQL 查车道行为。
    # 180 天 = artifact 窗口的两倍，够回看上一季度，又把它封在 ~82MB。
    (
        TABLE_REVIEW_SHADOW_LANE_DAILY,
        "trade_date",
        _int_env("DB_REVIEW_SHADOW_LANE_RETENTION_DAYS", 180),
        "iso_date",
    ),
    # 动态策略影子：单行 50KB。读取口径写死 30 天(load_policy_shadow_runs 的 days=30)，
    # 留 90 天是读窗的三倍，留足手动排查余量。
    (
        TABLE_SIGNAL_POLICY_SHADOW_RUNS,
        "trade_date",
        _int_env("DB_SHADOW_RUNS_RETENTION_DAYS", 90),
        "iso_date",
    ),
    # 题材雷达：单行 26KB，只有 load_latest_theme_radar_snapshot 一个读取口，取最新一份。
    (TABLE_THEME_RADAR_SNAPSHOT, "trade_date", _int_env("DB_THEME_RADAR_RETENTION_DAYS", 180), "iso_date"),
    # 概念热度：179B/行、31 行/日，本身不大；给规则只为不让它无界增长。
    (
        TABLE_CONCEPT_HEAT_HISTORY,
        "trade_date",
        _int_env("DB_CONCEPT_HEAT_RETENTION_DAYS", 180),
        "iso_date",
    ),
    # ↓ 下面两张现在一行都删不掉(最早数据 2026-05-25，不满 365 天)，是**增长封顶**不是清理。
    # 它们是原始事实、不可重算，且 eval 证据只落 artifact(90 天上限)时它们是唯一的长期证据，
    # 所以按 factor_ic_daily 的先例取 365 天：够覆盖跨年度/跨 regime 比较，同时把
    # outcomes 封在 ~50MB、observations 封在 ~28MB 而不是无界。
    # observations 是 outcomes 的父表，删父行会级联删子行；两者同为 trade_date、同一 TTL，
    # 级联结果与各自规则一致。OPERATOR_PLAYBOOK 要求删 observation 后重跑
    # signal_feedback_job.py，那条针对的是删近期行：这里删的是一年前的行，所有 horizon 早已
    # 结算，health/registry 又只看近窗，故不需要重跑。
    (TABLE_SIGNAL_OBSERVATIONS, "trade_date", _int_env("DB_SIGNAL_OBSERVATION_RETENTION_DAYS", 365), "iso_date"),
    (TABLE_SIGNAL_OUTCOMES, "trade_date", _int_env("DB_SIGNAL_OUTCOME_RETENTION_DAYS", 365), "iso_date"),
]
RECOMMENDATION_KEEP_DATES = _int_env("DB_RECOMMENDATION_KEEP_DATES", 30)
RECOMMENDATION_DATE_PAGE_SIZE = 1000
RECOMMENDATION_TRACKING_TABLES = (
    TABLE_RECOMMENDATION_TRACKING,
    TABLE_RECOMMENDATION_TRACKING_US,
    TABLE_RECOMMENDATION_TRACKING_HK,
)


def run_db_maintenance(request: DbMaintenanceRequest) -> int:
    client = create_admin_client()
    all_ok = True
    for table, date_col, ttl_days, cutoff_kind in CLEANUP_RULES:
        status, count = cleanup_table(client, table, date_col, ttl_days, cutoff_kind, dry_run=request.dry_run)
        print(_table_status_line(table, status, count, ttl_days=ttl_days))
        if status.startswith("error"):
            all_ok = False

    for table in RECOMMENDATION_TRACKING_TABLES:
        status, count = cleanup_recommendation_table(client, table, dry_run=request.dry_run)
        print(_table_status_line(table, status, count))
        if status.startswith("error"):
            all_ok = False
    return 0 if all_ok else 1


def cleanup_table(
    client,
    table: str,
    date_col: str,
    ttl_days: int,
    cutoff_kind: str,
    *,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    cutoff = _cutoff_value(ttl_days, cutoff_kind)
    try:
        if dry_run:
            resp = client.table(table).select("*", count="exact").lt(date_col, cutoff).limit(0).execute()
            return "dry_run", resp.count or 0
        client.table(table).delete().lt(date_col, cutoff).execute()
        return "ok", None
    except Exception as e:
        return f"error: {e}", None


def cleanup_recommendation_table(
    client,
    table: str,
    *,
    keep_dates: int = RECOMMENDATION_KEEP_DATES,
    page_size: int = RECOMMENDATION_DATE_PAGE_SIZE,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    keep_dates = max(int(keep_dates), 1)
    page_size = max(int(page_size), 1)
    dates = _latest_recommend_dates(client, table, keep_dates, page_size)
    if len(dates) < keep_dates:
        count = 0 if dry_run else None
        return f"keep_all, keep_dates={keep_dates}, distinct_dates={len(dates)}", count

    cutoff = dates[keep_dates - 1]
    try:
        if dry_run:
            resp = client.table(table).select("*", count="exact").lt("recommend_date", cutoff).limit(0).execute()
            return f"dry_run, keep_dates={keep_dates}, cutoff={cutoff}", resp.count or 0
        client.table(table).delete().lt("recommend_date", cutoff).execute()
        return f"ok, keep_dates={keep_dates}, cutoff={cutoff}", None
    except Exception as e:
        return f"error: {e}", None


def cleanup_recommendation_tracking(
    client,
    *,
    keep_dates: int = RECOMMENDATION_KEEP_DATES,
    page_size: int = RECOMMENDATION_DATE_PAGE_SIZE,
    dry_run: bool = False,
) -> tuple[str, int | None]:
    return cleanup_recommendation_table(
        client,
        TABLE_RECOMMENDATION_TRACKING,
        keep_dates=keep_dates,
        page_size=page_size,
        dry_run=dry_run,
    )


def _cutoff_value(ttl_days: int, kind: str) -> str | int:
    d = (datetime.now(UTC) - timedelta(days=ttl_days)).date()
    if kind == "yyyymmdd_int":
        return int(d.strftime("%Y%m%d"))
    return d.isoformat()


def _latest_recommend_dates(client, table: str, keep_dates: int, page_size: int) -> list[int]:
    dates: list[int] = []
    seen: set[int] = set()
    before_date: int | None = None

    while len(dates) < keep_dates:
        query = client.table(table).select("recommend_date").order("recommend_date", desc=True).limit(page_size)
        if before_date is not None:
            query = query.lt("recommend_date", before_date)

        rows = query.execute().data or []
        valid_dates = [d for row in rows if (d := _to_int_date(row.get("recommend_date"))) is not None]
        if not valid_dates:
            break

        for recommend_date in valid_dates:
            if recommend_date not in seen:
                seen.add(recommend_date)
                dates.append(recommend_date)
                if len(dates) >= keep_dates:
                    break

        before_date = min(valid_dates)

    return dates


def _to_int_date(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _table_status_line(table: str, status: str, count: int | None, *, ttl_days: int | None = None) -> str:
    ttl_text = f", ttl={ttl_days}d" if ttl_days is not None else ""
    suffix = f" ({count} rows)" if count is not None else ""
    return f"[db_maintenance] {table}: {status}{ttl_text}{suffix}"
