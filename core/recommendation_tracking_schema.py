"""``recommendation_tracking`` 缺失可选列的补列语句。

本项目不保留 .sql 文件（``scripts/quality_gate.py --check-no-sql`` 会拦），且 Supabase
Python SDK 走 REST 不支持 DDL、环境也无 psycopg2/asyncpg 与连接串，故 schema 以 Python
常量形式版本化，由 ``scripts/print_recommendation_tracking_ddl.py`` 打印后人工在 SQL
Editor 执行一次。与 ``core/factor_ic_schema.py``、``core/shadow_ledger_schema.py``
同一套做法。

这里只补列、不建表：``recommendation_tracking`` 早已存在（实测 76 列），是写入侧的
``RECOMMENDATION_OPTIONAL_COLUMNS`` 长出了三列而生产表没跟上。

## 为什么只有这三列缺

``integrations/recommendation_payload.py`` 的 ``upsert_recommendation_payload_rows``
在 400 之后会剔掉报错列重试，所以缺列不会让写入失败 —— 但也因此没人会注意到。
2026-08-31 那轮日志里 ``POST /rest/v1/recommendation_tracking ... 400`` 紧跟一条
去掉 ``capital_migration_bonus`` 的 ``201 Created``，就是这个降级在生效。

实测（2026-09-01 对生产表取一行比对 56 个可选列）缺的是：

- ``capital_migration_bonus`` —— **不只是审计损失**。``workflows/step4_from_supabase.py``
  的 ``fetch_recommendation_rows`` 把它写进了 ``select`` 列表，那条读路径**硬失败**：
  ``column recommendation_tracking.capital_migration_bonus does not exist``（42703）。
  该工作流只有 ``workflow_dispatch``、最近一次成功运行是 2026-05-17，所以定时链路没受
  影响；但只要有人手动跑「从 Supabase 复用今日候选」就会直接报错。
- ``dynamic_shadow_score`` / ``dynamic_shadow_promotion`` —— 审计损失。同一轮内
  Step2→Step3 靠 ``daily_job_persistence`` 的内存 dict 传递（见
  ``_dynamic_shadow_review_symbols``），不回读这张表，所以当轮判读不受影响；
  丢的是跨轮复盘「动态影子晋级当时给了多少分、卡在哪一条」的证据。

补完这三列后，那条 400 → 201 的降级路径不再被触发（机制保留，作为下次加列的兜底）。
"""

from __future__ import annotations

from core.constants import TABLE_RECOMMENDATION_TRACKING

# (列名, 类型, 注释)。类型与表内同类列对齐：分数用 numeric，结构化 payload 用 jsonb
# （实测 candidate_metrics / candidate_reasons 回来就是 dict）。
MISSING_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "capital_migration_bonus",
        "numeric",
        "资金迁移加分。step4_from_supabase 的读路径 select 了它，缺列会 42703 硬失败",
    ),
    (
        "dynamic_shadow_score",
        "numeric",
        "动态影子晋级分。仅 Step3 复核资格用，跨轮复盘需要",
    ),
    (
        "dynamic_shadow_promotion",
        "jsonb",
        "动态影子晋级判定详情：status/eligible/checks/blockers",
    ),
)


def build_ddl() -> str:
    """生成补列语句。幂等——``add column if not exists`` 可重复执行。"""
    lines = [
        f"-- 补 public.{TABLE_RECOMMENDATION_TRACKING} 的三个缺失可选列。",
        "-- 写入侧有「剔掉报错列重试」的降级，所以缺列此前只表现为静默丢字段；",
        "-- 但 capital_migration_bonus 同时在 step4_from_supabase 的 select 里，那条读路径是硬失败。",
        "",
        f"alter table public.{TABLE_RECOMMENDATION_TRACKING}",
    ]
    clauses = [f"    add column if not exists {name} {ddl_type}" for name, ddl_type, _ in MISSING_COLUMNS]
    lines.append(",\n".join(clauses) + ";")
    lines.append("")
    for name, _, comment in MISSING_COLUMNS:
        lines.append(f"comment on column public.{TABLE_RECOMMENDATION_TRACKING}.{name} is")
        lines.append(f"    '{comment}';")
    return "\n".join(lines)


def column_names() -> frozenset[str]:
    return frozenset(name for name, _, _ in MISSING_COLUMNS)
