"""theme_structure_cross_daily 的建表语句。

这张表回答一个问题：当日威科夫漏斗里偏强的票，有没有同时落在
ashare-mainline-radar 当日 rank≤5 的策划主题里。它是筛选观察，不是
recommendation_tracking / Step4 的买点源，也不写 next_buy。

为什么单独建表，而不是扩 review_capture_daily / review_shadow_lane_daily：
- review_capture_daily 的粒度和问题是「今日 >7% 里前一日漏斗捕获了谁」；
- review_shadow_lane_daily 的键是 (trade_date, ts_code, lane)，记的是影子车道；
- 交叉行要带 Radar 主题/角色/计划字段，硬塞进那两张表会把两种复盘口径搅在一起。

本项目不保留 .sql 文件，Supabase Python SDK 也不做 DDL，故 schema 以常量
版本化，由 scripts/print_theme_structure_cross_ddl.py 打印后在 SQL Editor 执行。
"""

from __future__ import annotations

from core.constants import TABLE_THEME_STRUCTURE_CROSS_DAILY

COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("trade_date", "date not null", "信号日（漏斗运行的交易日），非写入日"),
    ("ts_code", "text not null", "股票代码，6 位数字"),
    ("name", "text", "股票名称，便于人工核对"),
    ("wy_lane", "text", "威科夫车道 slug，如 mainline / sos / spring"),
    ("wy_status", "text", "威科夫候选状态，如 主线观察 / formal_l4"),
    ("wy_source", "text", "selection_source，可带 :market_blocked"),
    ("wy_score", "numeric(10, 4)", "漏斗分/优先级分，缺则 null"),
    ("gate_blocked", "boolean not null default false", "市场闸门拦截。true 仍落库，只记不执行"),
    ("cohort", "text not null", "primary=默认展示；broad=对照用宽口径"),
    ("radar_theme", "text not null", "Radar primary_theme，来自策划篮子，不是东财热榜"),
    ("theme_rank", "integer not null", "当日 radar_theme_snapshots.rank，v1 只收 ≤5"),
    ("theme_status", "text", "主题 status / lifecycle_stage"),
    ("radar_roles", "text", "Radar roles，原文序列化"),
    ("action_state", "text", "Radar action_state"),
    ("has_radar_plan", "boolean not null default false", "当日 radar_trade_plans 是否有该票"),
    ("created_at", "timestamptz not null default now()", "写入时间"),
)

UNIQUE_KEY = ("trade_date", "ts_code")


def payload_keys() -> frozenset[str]:
    return frozenset(name for name, _, _ in COLUMNS if name != "created_at")


def build_ddl() -> str:
    table = TABLE_THEME_STRUCTURE_CROSS_DAILY
    body = ["    id bigserial primary key,"]
    for name, ddl_type, comment in COLUMNS:
        body.append(f"    -- {comment}")
        body.append(f"    {name} {ddl_type},")
    body.append(f"    constraint {table}_unique unique ({', '.join(UNIQUE_KEY)})")
    return "\n".join(
        [
            f"create table if not exists public.{table} (",
            *body,
            ");",
            "",
            f"create index if not exists {table}_date_cohort_idx",
            f"    on public.{table} (trade_date desc, cohort);",
            "",
            f"create index if not exists {table}_code_idx",
            f"    on public.{table} (ts_code, trade_date desc);",
            "",
        ]
    )
