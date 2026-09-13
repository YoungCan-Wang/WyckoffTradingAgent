"""daily_nav 当日盈亏列。表本身已存在；此处只版本化新增列。

本项目不保留 .sql 文件，Supabase Python SDK 也不走 DDL，故由
scripts/print_daily_nav_ddl.py 打印后在 SQL Editor 执行一次。
"""

from __future__ import annotations

from core.constants import TABLE_DAILY_NAV

# 与 upsert_daily_nav 在算全当日盈亏时写入的可选键一致。
DAY_PNL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("day_pnl", "numeric", "当日盈亏金额（人民币）=各持仓 vs 昨收之和（昨收缺失才 vs 今开成本）；现金记 0"),
    ("day_pnl_pct", "numeric", "当日盈亏占日初权益（日初市值+现金）的百分比"),
    (
        "position_day_pnl",
        "jsonb not null default '[]'::jsonb",
        "当前持仓逐只当日盈亏，不写全市场",
    ),
)

DAY_PNL_KEYS = frozenset(name for name, _ddl, _note in DAY_PNL_COLUMNS)


def build_ddl() -> str:
    """幂等 ALTER：可重复执行，不改已有净值行。"""
    lines = [
        f"-- {TABLE_DAILY_NAV} 当日盈亏列。由 core/daily_nav_schema.py 生成，请勿手改。",
        "",
    ]
    for name, spec, note in DAY_PNL_COLUMNS:
        lines.append(f"alter table public.{TABLE_DAILY_NAV} add column if not exists {name} {spec};")
        lines.append(f"comment on column public.{TABLE_DAILY_NAV}.{name} is '{note}';")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
