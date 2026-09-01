"""``signal_policy_shadow_runs`` 缺失列的补列语句。

本项目不保留 .sql 文件（``scripts/quality_gate.py --check-no-sql`` 会拦），且 Supabase
Python SDK 走 REST 不支持 DDL、环境也无 psycopg2/asyncpg 与连接串，故 schema 以 Python
常量形式版本化，由 ``scripts/print_signal_policy_shadow_ddl.py`` 打印后人工在 SQL
Editor 执行一次。与 ``core/recommendation_tracking_schema.py``、``core/factor_ic_schema.py``
同一套做法。

这里只补列、不建表：``signal_policy_shadow_runs`` 早已存在（实测 21 列）。

## 影子账本为什么停在 2026-07-01

``strategy_policy_governor`` 的归因重算长期报 ``insufficient_shadow_sample``
（``MIN_SHADOW_RUNS = 10`` / ``MIN_SHADOW_MATCHED = 3``），一直被读成"门槛太严"。
实测这张表**只有 24 行、最新一行是 2026-07-01**，即写入方两个月前就停了，门槛是症状
不是原因。

停写时点与代码改动严格对应：``workflows/funnel_ai_selection.py`` 的
``_policy_shadow_row`` 在 2026-07-04（``4ec40f05`` / ``e098a494``）新增了
``attribution_signal_weights`` 与 ``attribution_policy_meta`` 两个键，而生产表没跟上。
两者一起决定了此后每次写入都是 42703：

    column signal_policy_shadow_runs.attribution_signal_weights does not exist

而 ``upsert_policy_shadow_run`` 用的是 ``raise_on_error=False``，异常只落到
``logger.warning``，日志里那行 ``动态策略shadow已写入 ... written=0`` 看着像正常输出。
于是漏斗每天照跑、影子对照一行没进、归因重算永久卡在 insufficient_sample，两个月无人
察觉。

对照 ``recommendation_tracking`` 那次：写入侧有"剔掉报错列重试"的降级，缺列只丢字段
不丢行。这张表没有那层降级，缺列直接丢整行——同一种 schema 漂移，后果重一个量级。
本次同时给 ``_execute_upsert`` 补上按表登记的可选列降级（见
``OPTIONAL_COLUMNS_BY_TABLE``），下次再加列最坏只丢字段。
"""

from __future__ import annotations

from core.constants import TABLE_SIGNAL_POLICY_SHADOW_RUNS

# (列名, 类型, 注释)。类型与表内同类列对齐：结构化 payload 用 jsonb
# （实测 base_policy / signal_weights 回来就是 dict）。
MISSING_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "attribution_signal_weights",
        "jsonb",
        "归因产出的信号权重（shadow 口径）。缺列会让整行写入 42703 失败",
    ),
    (
        "attribution_policy_meta",
        "jsonb",
        "归因快照元信息：report_date/next_action/source/weight_count 等",
    ),
)


def build_ddl() -> str:
    """生成补列语句。幂等——``add column if not exists`` 可重复执行。"""
    lines = [
        f"-- 补 public.{TABLE_SIGNAL_POLICY_SHADOW_RUNS} 的两个缺失列。",
        "-- 2026-07-04 起写入侧新增这两个键，生产表未跟上，此后每次 upsert 都 42703；",
        "-- 且该写入是 raise_on_error=False，异常只进 logger.warning，两个月静默无产出。",
        "",
        f"alter table public.{TABLE_SIGNAL_POLICY_SHADOW_RUNS}",
    ]
    clauses = [f"    add column if not exists {name} {ddl_type}" for name, ddl_type, _ in MISSING_COLUMNS]
    lines.append(",\n".join(clauses) + ";")
    lines.append("")
    for name, _, comment in MISSING_COLUMNS:
        lines.append(f"comment on column public.{TABLE_SIGNAL_POLICY_SHADOW_RUNS}.{name} is")
        lines.append(f"    '{comment}';")
    return "\n".join(lines)


def column_names() -> frozenset[str]:
    return frozenset(name for name, _, _ in MISSING_COLUMNS)
